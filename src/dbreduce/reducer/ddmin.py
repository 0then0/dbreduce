from collections.abc import Iterator

from dbreduce.models.schema import RowKey


def chunks(rows: list[RowKey], size: int) -> Iterator[list[RowKey]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def sizes(count: int) -> Iterator[int]:
    """Try a whole table, then progressively smaller chunks, down to single rows."""
    while count > 0:
        yield count
        if count == 1:
            break
        count = (count + 1) // 2
