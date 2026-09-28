import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager


class Performance:
    def __init__(self) -> None:
        self.phases: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))

    @contextmanager
    def measure(self, operation: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            count, total = self.phases[operation]
            self.phases[operation] = count + 1, total + time.monotonic() - started

    def report(self) -> dict[str, dict[str, float | int]]:
        return {
            operation: {
                "count": count,
                "total_seconds": total,
                "average_seconds": total / count,
            }
            for operation, (count, total) in sorted(self.phases.items())
        }
