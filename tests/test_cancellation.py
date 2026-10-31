"""Cancellation without PostgreSQL: signals, shutdown budget, owned subprocesses, oracle."""

import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from dbreduce.cancellation import Cancelled, Leftover, RunControl, current
from dbreduce.oracle.runner import Oracle
from dbreduce.processes import run_owned

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX signals and process groups")


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    if status.exists():
        return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    return True


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def send_to_main_thread_when(path: Path, signum: int = signal.SIGTERM) -> threading.Thread:
    """Deliver ``signum`` to the main thread once ``path`` exists (an observed stage)."""

    def run() -> None:
        wait_for(path.exists)
        signal.pthread_kill(threading.main_thread().ident, signum)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def control():
    run = RunControl(budget=30.0)
    run.start()
    try:
        yield run
    finally:
        run.stop()


@posix_only
def test_import_installs_no_signal_handlers():
    code = (
        "import signal, dbreduce.cli, dbreduce.cancellation;"
        "print(signal.getsignal(signal.SIGTERM) is signal.SIG_DFL,"
        " signal.getsignal(signal.SIGINT) is signal.default_int_handler)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.split() == ["True", "True"], result.stderr


@posix_only
def test_handlers_are_installed_for_a_run_and_restored():
    before = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    run = RunControl()
    run.start()
    try:
        assert signal.getsignal(signal.SIGTERM) != before[signal.SIGTERM]
        assert signal.getsignal(signal.SIGINT) != before[signal.SIGINT]
    finally:
        run.stop()
    assert {n: signal.getsignal(n) for n in before} == before
    assert current() is not run


@posix_only
def test_handlers_are_not_installed_outside_the_main_thread():
    before = signal.getsignal(signal.SIGTERM)
    seen = []

    def start_and_stop():
        run = RunControl()
        run.start()
        seen.append(signal.getsignal(signal.SIGTERM))
        run.stop()

    thread = threading.Thread(target=start_and_stop)
    thread.start()
    thread.join()
    assert seen == [before]


@posix_only
def test_signal_raises_cancelled_once_and_repeats_are_ignored(control):
    with pytest.raises(Cancelled) as raised:
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
    assert raised.value.signum == signal.SIGTERM
    assert isinstance(raised.value, KeyboardInterrupt)
    assert control.signal_name == "SIGTERM"
    assert control.remaining() is not None and 0 < control.remaining() <= 30.0
    # A second signal while cleanup runs is recorded, not raised.
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.2)
    assert control.ignored_signals == 1


@posix_only
def test_sigint_cancels_like_sigterm(control):
    with pytest.raises(Cancelled) as raised:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)
    assert raised.value.signum == signal.SIGINT


@posix_only
def test_protected_section_defers_cancellation_until_it_ends(control):
    steps = []
    with pytest.raises(Cancelled):
        with control.protected():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
            steps.append("section finished")
        steps.append("not reached")
    assert steps == ["section finished"]
    # Once delivered, later protected sections (cleanup) are not interrupted again.
    with control.protected():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)
    assert control.ignored_signals == 1


@posix_only
def test_no_new_work_after_cancellation(control):
    with pytest.raises(Cancelled):  # delivered when the protected section ends
        with control.protected():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)
    with pytest.raises(Cancelled):
        control.checkpoint()
    with pytest.raises(Cancelled):
        run_owned([sys.executable, "-c", "pass"])


@posix_only
def test_signal_after_commit_does_not_cancel(control):
    control.commit()
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.2)
    assert control.cancelled and control.committed


def test_budget_shrinks_timeouts_and_expires():
    run = RunControl(budget=100.0)
    assert run.bounded(600) == 600 and not run.expired()
    run.requested = signal.SIGTERM
    run.deadline = time.monotonic() + 3
    assert 1.0 <= run.bounded(600) <= 3.0
    run.deadline = time.monotonic() - 1
    assert run.expired() and run.bounded(600) == 1.0


@posix_only
def test_watchdog_forces_exit_when_cleanup_never_returns(tmp_path):
    script = textwrap.dedent(
        """
        import subprocess, sys, time
        import dbreduce.cancellation as c
        from dbreduce.cancellation import Leftover, RunControl
        c.REPORT_MARGIN = 0.5
        run = RunControl(budget=1.0)
        run.start()
        run.register(lambda: [Leftover("database", "dbreduce_example", "removal not attempted")])
        owned = subprocess.Popen(["sleep", "60"], start_new_session=True)
        run.track_group(owned.pid)
        print(owned.pid, flush=True)
        with run.protected():  # cleanup that blocks forever
            time.sleep(120)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    owned_pid = int(process.stdout.readline())
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    _, err = process.communicate(timeout=15)
    assert time.monotonic() - started < 6
    assert process.returncode == 1
    assert "forcing exit" in err and "NOT confirmed" in err
    assert "dbreduce_example" in err
    wait_for(lambda: not alive(owned_pid), timeout=5)


@posix_only
def test_run_owned_timeout_kills_group_without_waiting_for_pipes(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    command = f"sleep 60 & echo $! > {shlex.quote(str(pidfile))}; wait"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_owned(["sh", "-c", command], timeout=1.0)
    assert time.monotonic() - started < 8
    grandchild = int(pidfile.read_text())
    wait_for(lambda: not alive(grandchild), timeout=5)


@posix_only
@pytest.mark.parametrize("cooperative", [True, False])
def test_run_owned_cancellation_terminates_then_kills(control, tmp_path, cooperative):
    flag = tmp_path / "started"
    seen = tmp_path / "term-seen"
    script = textwrap.dedent(
        f"""
        import os, signal, sys, time
        def on_term(*_):
            open({str(seen)!r}, "w").close()
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term if {cooperative} else signal.SIG_IGN)
        open({str(flag)!r}, "w").write(str(os.getpid()))
        time.sleep(120)
        """
    )
    trigger = send_to_main_thread_when(flag)
    started = time.monotonic()
    with patch("dbreduce.processes.TERMINATE_GRACE", 1.0):
        with pytest.raises(Cancelled):
            run_owned([sys.executable, "-c", script], timeout=120)
    trigger.join()
    assert time.monotonic() - started < 10
    assert not alive(int(flag.read_text()))
    # The graceful SIGTERM reached a cooperative process before any SIGKILL.
    assert seen.exists() is cooperative


@posix_only
@pytest.mark.parametrize("cooperative", [True, False])
def test_oracle_cancellation_stops_its_process_group(control, tmp_path, cooperative):
    flag = tmp_path / "started"
    seen = tmp_path / "term-seen"
    child_pid = tmp_path / "child.pid"
    script = textwrap.dedent(
        f"""
        import os, signal, subprocess, sys, time
        def on_term(*_):
            open({str(seen)!r}, "w").close()
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term if {cooperative} else signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "120"])  # a grandchild of the supervisor
        open({str(child_pid)!r}, "w").write(str(child.pid))
        open({str(flag)!r}, "w").write(str(os.getpid()))
        time.sleep(120)
        """
    )
    # exec: the command itself is in the group, not a shell that would die on SIGTERM.
    ignore = "" if cooperative else "trap '' TERM; "
    oracle = Oracle(
        f"{ignore}exec {shlex.quote(sys.executable)} -c {shlex.quote(script)}", timeout=120
    )
    trigger = send_to_main_thread_when(flag)
    started = time.monotonic()
    with patch("dbreduce.oracle.runner.TERMINATE_GRACE", 1.0):
        with pytest.raises(Cancelled):
            oracle.fails("postgresql:///test", lambda: None)
    trigger.join()
    assert time.monotonic() - started < 10
    assert not alive(int(flag.read_text()))
    wait_for(lambda: not alive(int(child_pid.read_text())), timeout=5)
    assert seen.exists() is cooperative
    # Cancellation is not a verdict about the candidate.
    assert oracle.executions == 1 and not oracle.outcomes


@posix_only
def test_oracle_started_after_cancellation_is_refused(control):
    with pytest.raises(Cancelled):
        with control.protected():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)
    oracle = Oracle(f"{shlex.quote(sys.executable)} -c pass")
    with pytest.raises(Cancelled):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.executions == 0


def test_leftover_lines_name_each_resource():
    from dbreduce.cancellation import describe_leftovers

    lines = describe_leftovers([Leftover("database", "dbreduce_x", "result unknown", "why")])
    assert lines == ["  database dbreduce_x: result unknown", "    why"]


@posix_only
def test_inherited_ignore_disposition_is_kept():
    code = (
        "import os, signal, time\n"
        "from dbreduce.cancellation import RunControl\n"
        "run = RunControl(); run.start()\n"
        "kept = signal.getsignal(signal.SIGINT) == signal.SIG_IGN\n"
        "os.kill(os.getpid(), signal.SIGINT); time.sleep(0.3)\n"
        "print(kept, run.cancelled); run.stop()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
    )
    assert result.stdout.split() == ["True", "False"], result.stderr


@posix_only
def test_watchdog_kills_a_running_oracle_group(tmp_path):
    pidfile = tmp_path / "oracle.pid"
    script = textwrap.dedent(
        f"""
        import sys, time
        import dbreduce.cancellation as c
        from dbreduce.cancellation import RunControl
        from dbreduce.oracle.runner import Oracle
        c.REPORT_MARGIN = 0.5
        run = RunControl(budget=1.0)
        run.start()
        oracle = Oracle("echo $$ > {pidfile}; exec sleep 120", timeout=120)
        with run.protected():  # cancellation is deferred, so only the watchdog can end this
            oracle.fails("postgresql:///test", lambda: None)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    wait_for(pidfile.exists)
    oracle_pid = int(wait_for(lambda: pidfile.read_text().strip()))
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=15)
    assert process.returncode == 1
    wait_for(lambda: not alive(oracle_pid), timeout=5)
