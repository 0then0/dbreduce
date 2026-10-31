"""Subprocesses owned by this run: own process group, bounded stop, no pipe hangs."""

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from typing import IO, Any

from dbreduce.cancellation import TERMINATE_GRACE, current

# After SIGKILL the leader is reaped almost immediately; this only bounds a stuck kernel wait.
REAP_TIMEOUT = 2.0
# Windows has no SIGKILL; terminating is its only forceful stop.
KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _signal_group(pgid: int, number: signal.Signals) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pgid, number)
        else:  # pragma: no cover - non-POSIX
            os.kill(pgid, number)
    except OSError:
        pass  # Already gone.


def stop_group(process: "subprocess.Popen[Any]", *, grace: float = TERMINATE_GRACE) -> None:
    """Stop the process group started for ``process`` and reap its leader.

    Only groups created by ``start_new_session=True`` for this run are signalled. SIGTERM
    is sent first and the leader gets ``grace`` seconds (shortened by the shutdown budget);
    then the whole group is killed so no member keeps a pipe or a database session open.
    """
    pgid = process.pid
    if grace > 0:
        wait = current().bounded(grace)
        _signal_group(pgid, signal.SIGTERM)
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            pass
    _signal_group(pgid, KILL)
    try:
        process.wait(timeout=REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass


def close_pipes(process: "subprocess.Popen[Any]") -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def run_owned(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: int | IO[Any] | None = None,
    input: str | bytes | None = None,
    text: bool = False,
    timeout: float | None = None,
    check: bool = False,
) -> "subprocess.CompletedProcess[Any]":
    """Like ``subprocess.run(..., capture_output=True)`` for a process this run owns.

    On any interruption (timeout, cancellation) the whole process group is stopped and the
    pipes are closed without being read again, so a grandchild holding them cannot block.
    """
    control = current()
    control.checkpoint()
    process = subprocess.Popen(
        list(args),
        env=None if env is None else dict(env),
        stdin=subprocess.PIPE if input is not None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )
    control.track_group(process.pid)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException as error:
        # A timeout keeps the old immediate-kill behavior; cancellation gets a graceful stop.
        timed_out = isinstance(error, subprocess.TimeoutExpired)
        stop_group(process, grace=0 if timed_out else TERMINATE_GRACE)
        close_pipes(process)
        raise
    finally:
        control.untrack_group(process.pid)
    result = subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
