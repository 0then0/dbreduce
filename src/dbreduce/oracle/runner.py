import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable


class OracleError(RuntimeError):
    pass


_REGEX_MATCHER = """
import json
import re
import sys

data = json.load(sys.stdin)
matched = all(
    pattern is None or re.search(pattern, text) is not None
    for pattern, text in zip(data["patterns"], data["texts"], strict=True)
)
print(json.dumps(matched))
"""

_ORACLE_SUPERVISOR = """
import os
import signal
import subprocess
import sys

status_fd = int(os.environ.pop("DBREDUCE_ORACLE_STATUS_FD"))

def terminate_group(_signum, _frame):
    os.killpg(os.getpgrp(), signal.SIGKILL)

signal.signal(signal.SIGTERM, terminate_group)
child = subprocess.Popen(sys.argv[2], shell=True)
status = child.wait()
os.write(status_fd, str(status).encode("ascii"))
os.close(status_fd)
os.killpg(os.getpgrp(), signal.SIGKILL)
"""


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

    def _classify(
        self, code: int, stdout: str, stderr: str, *, matcher_timeout: float
    ) -> tuple[str, str | None]:
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
            if self.stdout_pattern is not None or self.stderr_pattern is not None:
                if matcher_timeout <= 0:
                    raise subprocess.TimeoutExpired("oracle matcher", self.timeout)
                try:
                    result = subprocess.run(
                        [sys.executable, "-c", _REGEX_MATCHER],
                        input=json.dumps(
                            {
                                "patterns": [
                                    self.stdout_pattern.pattern if self.stdout_pattern else None,
                                    self.stderr_pattern.pattern if self.stderr_pattern else None,
                                ],
                                "texts": [stdout, stderr],
                            }
                        ),
                        capture_output=True,
                        text=True,
                        timeout=matcher_timeout,
                        check=True,
                    )
                except subprocess.TimeoutExpired:
                    raise
                except (OSError, subprocess.CalledProcessError) as error:
                    raise OracleError("Could not evaluate oracle identity matchers") from error
                if json.loads(result.stdout) is not True:
                    return "different_failure", None
            identity = "any-nonzero" if self.mode == "legacy" else f"exit:{code}"
        if self.expected_signature is not None and identity != self.expected_signature:
            return "different_failure", identity
        return "same_failure", identity

    def _run(self, env: dict[str, str]) -> tuple[int, str, str]:
        capture_out = self.mode == "json" or self.stdout_pattern is not None
        capture_err = self.stderr_pattern is not None
        buffers = [bytearray(), bytearray()]
        status_read, status_write = os.pipe()
        os.set_inheritable(status_write, True)
        supervisor_env = {**env, "DBREDUCE_ORACLE_STATUS_FD": str(status_write)}
        deadline = time.monotonic() + self.timeout
        try:
            with subprocess.Popen(
                [sys.executable, "-c", _ORACLE_SUPERVISOR, str(status_write), self.command],
                shell=False,
                env=supervisor_env,
                start_new_session=True,
                pass_fds=(status_write,),
                stdout=subprocess.PIPE if capture_out else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_err else subprocess.DEVNULL,
            ) as process:
                os.close(status_write)
                status_data = bytearray()
                status_complete = False
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(status_read, selectors.EVENT_READ, "status")
                        for index, stream in enumerate((process.stdout, process.stderr)):
                            if stream is not None:
                                selector.register(stream, selectors.EVENT_READ, index)
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(self.command, self.timeout)
                            for key, _ in selector.select(min(remaining, 0.05)):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    if key.data == "status":
                                        status_complete = True
                                    continue
                                if key.data == "status":
                                    status_data.extend(chunk)
                                    if len(status_data) > 4:
                                        raise OracleError("Invalid oracle supervisor status")
                                    continue
                                buffer = buffers[key.data]
                                if len(buffer) + len(chunk) > 1024 * 1024:
                                    raise OracleError("Oracle output exceeds 1 MiB identity limit")
                                buffer.extend(chunk)
                    if not status_complete or not status_data:
                        raise OracleError("Oracle supervisor exited without a command status")
                    try:
                        code = int(status_data)
                    except ValueError as error:
                        raise OracleError("Invalid oracle supervisor status") from error
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except BaseException:
                    # Signal the unreaped supervisor PID. Its handler kills its
                    # process group while the group leader is still alive.
                    try:
                        process.send_signal(signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
        finally:
            os.close(status_read)
            try:
                os.close(status_write)
            except OSError:
                pass
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
                outcome, identity = self._classify(
                    code,
                    out,
                    err,
                    matcher_timeout=self.timeout - (time.monotonic() - started),
                )
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
