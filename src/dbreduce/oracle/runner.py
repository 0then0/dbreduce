import os
import signal
import subprocess
from collections.abc import Callable


class OracleError(RuntimeError):
    pass


class Oracle:
    def __init__(self, command: str, *, confirm: int = 1, timeout: float = 60) -> None:
        if confirm < 1 or timeout <= 0:
            raise ValueError("confirm and timeout must be positive")
        self.command = command
        self.confirm = confirm
        self.timeout = timeout
        self.executions = 0

    def fails(self, url: str, prepare: Callable[[], None]) -> bool:
        env = os.environ.copy()
        env.update(DATABASE_URL=url, DBREDUCE_DATABASE_URL=url)
        for _ in range(self.confirm):
            prepare()
            self.executions += 1
            with subprocess.Popen(
                self.command,
                shell=True,
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    code = process.wait(timeout=self.timeout)
                except BaseException as error:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    if isinstance(error, subprocess.TimeoutExpired):
                        raise OracleError(
                            "Oracle timed out; this is not a reproduced failure"
                        ) from error
                    raise
                finally:
                    # Do not leave background children connected to the disposable database.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if code == 0:
                return False
        return True
