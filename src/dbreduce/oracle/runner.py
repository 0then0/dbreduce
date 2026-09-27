import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable


class OracleError(RuntimeError):
    pass


class Oracle:
    def __init__(
        self,
        command: str,
        *,
        confirm: int = 1,
        timeout: float = 60,
        match_stdout: str | None = None,
        match_stderr: str | None = None,
        expected_exit_code: int | None = None,
        structured: bool = False,
    ) -> None:
        if confirm < 1 or timeout <= 0:
            raise ValueError("confirm and timeout must be positive")
        if structured and any(
            x is not None for x in (match_stdout, match_stderr, expected_exit_code)
        ):
            raise ValueError("--oracle-json cannot be combined with exit/output matchers")
        if expected_exit_code is not None and not 1 <= expected_exit_code < 126:
            raise ValueError("Expected exit code must be between 1 and 125")
        self.stdout_pattern = re.compile(match_stdout) if match_stdout is not None else None
        self.stderr_pattern = re.compile(match_stderr) if match_stderr is not None else None
        self.mode = (
            "json"
            if structured
            else "matcher"
            if any(x is not None for x in (match_stdout, match_stderr, expected_exit_code))
            else "legacy"
        )
        self.command = command
        self.confirm = confirm
        self.timeout = timeout
        self.expected_exit_code = expected_exit_code
        self.expected_signature: str | None = None
        self.final_signature: str | None = None
        self.executions = 0
        self.seconds = 0.0
        self.outcomes: Counter[str] = Counter()
        self.last_outcome = "not_run"

    def _classify(self, code: int, stdout: str, stderr: str) -> tuple[str, str | None]:
        if code < 0 or code >= 126:
            raise OracleError("Oracle infrastructure error: launch failure or signal status")
        if self.mode == "json":
            if code != 0:
                raise OracleError("Structured oracle must exit zero with a verdict")
            try:
                verdict = json.loads(stdout)
            except ValueError as error:
                raise OracleError("Invalid structured oracle JSON") from error
            if not isinstance(verdict, dict) or type(verdict.get("reproduced")) is not bool:
                raise OracleError("Structured oracle requires a boolean reproduced field")
            if verdict.get("error") is not None:
                raise OracleError("Structured oracle reported an infrastructure error")
            if not verdict["reproduced"]:
                return "passed", None
            signature = verdict.get("signature")
            if not isinstance(signature, str) or not signature or len(signature) > 256:
                raise OracleError("Structured oracle requires a nonempty signature (max 256 chars)")
            # Store a digest, never arbitrary application output in reports.
            identity = "sha256:" + hashlib.sha256(signature.encode()).hexdigest()
        else:
            if code == 0:
                return "passed", None
            if self.expected_exit_code is not None and code != self.expected_exit_code:
                return "different_failure", None
            for pattern, output in ((self.stdout_pattern, stdout), (self.stderr_pattern, stderr)):
                if pattern is not None and pattern.search(output) is None:
                    return "different_failure", None
            identity = "any-nonzero" if self.mode == "legacy" else f"exit:{code}"
        if self.expected_signature is not None and identity != self.expected_signature:
            return "different_failure", identity
        return "same_failure", identity

    def _run(self, env: dict[str, str]) -> tuple[int, str, str]:
        capture_out = self.mode == "json" or self.stdout_pattern is not None
        capture_err = self.stderr_pattern is not None
        buffers = [bytearray(), bytearray()]
        deadline = time.monotonic() + self.timeout
        with subprocess.Popen(
            self.command,
            shell=True,
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE if capture_out else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_err else subprocess.DEVNULL,
        ) as process:

            def kill_group() -> None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            try:
                with selectors.DefaultSelector() as selector:
                    for index, stream in enumerate((process.stdout, process.stderr)):
                        if stream is not None:
                            selector.register(stream, selectors.EVENT_READ, index)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(self.command, self.timeout)
                        if process.poll() is not None:
                            # Children may still hold a pipe after the shell exits.
                            kill_group()
                        for key, _ in selector.select(min(remaining, 0.05)):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            buffer = buffers[key.data]
                            if len(buffer) + len(chunk) > 1024 * 1024:
                                raise OracleError("Oracle output exceeds 1 MiB identity limit")
                            buffer.extend(chunk)
                    code = process.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                kill_group()
                process.wait()
        return (
            code,
            buffers[0].decode("utf-8", errors="replace"),
            buffers[1].decode("utf-8", errors="replace"),
        )

    def fails(self, url: str, prepare: Callable[[], None]) -> bool:
        env = os.environ.copy()
        env.update(DATABASE_URL=url, DBREDUCE_DATABASE_URL=url)
        for _ in range(self.confirm):
            prepare()
            self.executions += 1
            started = time.monotonic()
            try:
                code, out, err = self._run(env)
                outcome, identity = self._classify(code, out, err)
            except subprocess.TimeoutExpired as error:
                self.last_outcome = "timeout"
                self.outcomes[self.last_outcome] += 1
                raise OracleError("Oracle timed out; this is not a reproduced failure") from error
            except (OSError, OracleError) as error:
                self.last_outcome = "infrastructure_error"
                self.outcomes[self.last_outcome] += 1
                raise OracleError(
                    str(error) if isinstance(error, OracleError) else "Could not launch oracle"
                ) from error
            finally:
                self.seconds += time.monotonic() - started
            self.last_outcome = outcome
            self.outcomes[outcome] += 1
            self.final_signature = identity
            if outcome != "same_failure":
                return False
            if self.expected_signature is None:
                self.expected_signature = identity
        return True

    def identity_report(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "expected_signature": self.expected_signature,
            "final_signature": self.final_signature,
            "preserved": self.last_outcome == "same_failure",
            "expected_exit_code": self.expected_exit_code,
            "stdout_regex": self.stdout_pattern.pattern if self.stdout_pattern else None,
            "stderr_regex": self.stderr_pattern.pattern if self.stderr_pattern else None,
        }
