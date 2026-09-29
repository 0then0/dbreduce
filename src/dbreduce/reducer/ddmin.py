from collections.abc import Iterator
from typing import TypeVar

T = TypeVar("T")


def chunks(rows: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def sizes(count: int) -> Iterator[int]:
    """Try a whole table, then progressively smaller chunks, down to single rows."""
    while count > 0:
        yield count
        if count == 1:
            break
        count = (count + 1) // 2
