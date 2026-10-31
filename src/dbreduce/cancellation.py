"""Cooperative cancellation and a finite shutdown budget for one CLI run.

A signal handler never cleans up. It records the request, starts the shutdown clock and
raises ``Cancelled`` (a ``KeyboardInterrupt``) so the existing ``finally`` and ``__exit__``
blocks unwind and release what the run owns. Cleanup code runs inside ``protected()`` so a
repeated signal cannot abort it. A watchdog thread bounds the whole shutdown even when a
blocking call never returns.
"""

import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any

# Seconds from the first cancelling signal until the process exits. It covers stopping
# owned subprocesses and every cleanup step. Docker's default stop timeout is 10 seconds,
# so containers need ``--stop-timeout`` above this value to let cleanup finish.
SHUTDOWN_BUDGET = 30.0
# Extra seconds after the budget for the watchdog to write the report before it forces exit.
REPORT_MARGIN = 2.0
# Upper bound between SIGTERM and SIGKILL for a process group started by this run.
TERMINATE_GRACE = 5.0

CANCEL_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class Cancelled(KeyboardInterrupt):
    """Raised in the main thread when a cancelling signal arrives."""

    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


@dataclass(frozen=True)
class Leftover:
    """A resource owned by this run whose removal is not confirmed."""

    kind: str  # "database" or "directory"
    name: str
    state: str
    detail: str = ""


def _notice(message: str) -> None:
    # os.write is safe inside a signal handler; buffered stderr may be mid-write.
    try:
        os.write(2, (message + "\n").encode("utf-8", errors="replace"))
    except OSError:
        pass


def describe_leftovers(leftovers: list[Leftover]) -> list[str]:
    lines = []
    for item in leftovers:
        lines.append(f"  {item.kind} {item.name}: {item.state}")
        if item.detail:
            lines.append(f"    {item.detail}")
    return lines


class RunControl:
    """Signal state, shutdown deadline and the resources a run must report."""

    def __init__(self, budget: float | None = None) -> None:
        self.budget = SHUTDOWN_BUDGET if budget is None else budget
        self.requested: int | None = None
        self.deadline: float | None = None
        self.committed = False
        self.ignored_signals = 0
        self.cleaned: Counter[str] = Counter()
        self._delivered = False
        self._protected = 0
        self._active = False
        self._sources: list[Callable[[], list[Leftover]]] = []
        self._groups: set[int] = set()
        self._previous: dict[int, Any] = {}
        self._signalled = threading.Event()
        self._finished = threading.Event()
        self._exit_lock = threading.Lock()
        self._watchdog: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Install handlers and the watchdog. Does nothing outside the main thread."""
        global _ACTIVE
        if _ACTIVE is not None:
            return
        _ACTIVE = self
        self._active = True
        if threading.current_thread() is not threading.main_thread():
            return
        for number in CANCEL_SIGNALS:
            if signal.getsignal(number) == signal.SIG_IGN:
                continue  # inherited "ignore" (nohup, background job) is the caller's choice
            self._previous[number] = signal.signal(number, self._handle)
        self._watchdog = threading.Thread(target=self._watch, name="dbreduce-watchdog", daemon=True)
        self._watchdog.start()

    def finish(self) -> None:
        """Mark cooperative shutdown as done so the watchdog stands down."""
        with self._exit_lock:
            self._finished.set()

    def stop(self) -> None:
        """Restore the handlers that were installed before ``start``."""
        global _ACTIVE
        self.finish()
        for number, handler in self._previous.items():
            signal.signal(number, handler)
        self._previous.clear()
        if _ACTIVE is self:
            _ACTIVE = None
        self._active = False

    # -- signal handling -------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self.requested is not None

    @property
    def signal_name(self) -> str:
        return signal.Signals(self.requested).name if self.requested is not None else ""

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.requested is not None:
            self.ignored_signals += 1
            _notice(
                f"Received {signal.Signals(signum).name} again; cleanup is already running and "
                "is not interrupted. To abandon it at once send SIGQUIT (Ctrl-\\) or SIGKILL; "
                "owned resources may then remain."
            )
            return
        self.requested = signum
        self.deadline = time.monotonic() + self.budget
        self._signalled.set()
        _notice(
            f"Received {signal.Signals(signum).name}: stopping and cleaning up resources owned "
            f"by this run (at most {self.budget:g} s)."
        )
        if self.committed or self._protected:
            return
        self._delivered = True
        raise Cancelled(signum)

    @contextmanager
    def protected(self) -> Iterator[None]:
        """Defer cancellation while releasing a resource; deliver it on leaving the section."""
        self._protected += 1
        try:
            yield
        finally:
            self._protected -= 1
            if (
                not self._protected
                and self.requested is not None
                and not self._delivered
                and not self.committed
            ):
                self._delivered = True
                raise Cancelled(self.requested)

    def checkpoint(self) -> None:
        """Refuse to start new work once cancellation was requested."""
        if self.requested is not None and not self.committed:
            self._delivered = True
            raise Cancelled(self.requested)

    def commit(self) -> None:
        """Past this point the result is published and later signals no longer cancel."""
        if self._active:
            self.committed = True

    # -- shutdown budget -------------------------------------------------------------

    def remaining(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def expired(self) -> bool:
        left = self.remaining()
        return left is not None and left <= 0

    def bounded(self, default: float) -> float:
        """Shrink a timeout to the time left in the shutdown budget (at least one second)."""
        left = self.remaining()
        return default if left is None else max(1.0, min(default, left))

    # -- ownership reporting ---------------------------------------------------------

    def register(self, source: Callable[[], list[Leftover]]) -> None:
        if self._active:
            self._sources.append(source)

    def leftovers(self) -> list[Leftover]:
        found: list[Leftover] = []
        for source in self._sources:
            try:
                found.extend(source())
            except Exception:
                found.append(Leftover("resource", "unknown", "could not be inspected"))
        return found

    def record_cleanup(self, status: str) -> None:
        """Count confirmed removals that happened after cancellation was requested."""
        if self.requested is not None:
            self.cleaned[status] += 1

    def track_group(self, pgid: int) -> None:
        self._groups.add(pgid)

    def untrack_group(self, pgid: int) -> None:
        self._groups.discard(pgid)

    # -- watchdog --------------------------------------------------------------------

    def _watch(self) -> None:
        # Cancelling signals must reach the main thread, never this one.
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_BLOCK, CANCEL_SIGNALS)
        self._signalled.wait()
        assert self.deadline is not None
        if self._finished.wait(max(0.0, self.deadline - time.monotonic()) + REPORT_MARGIN):
            return
        with self._exit_lock:
            if self._finished.is_set():
                return
            lines = [
                f"Shutdown budget of {self.budget:g} s exhausted after {self.signal_name}; "
                "forcing exit. Cleanup is NOT confirmed."
            ]
            leftovers = self.leftovers()
            if leftovers:
                lines.append("These resources owned by this run may remain:")
                lines.extend(describe_leftovers(leftovers))
            _notice("\n".join(lines))
            for pgid in list(self._groups):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            os._exit(1)


_ACTIVE: RunControl | None = None
_IDLE = RunControl()


def current() -> RunControl:
    """The control of the running CLI command; an inert one for library use."""
    return _ACTIVE or _IDLE
