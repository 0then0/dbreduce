from collections.abc import Callable
from typing import Protocol

from dbreduce.models.schema import RowKey, State, TableKey
from dbreduce.reducer.ddmin import chunks, sizes


class Backend(Protocol):
    def state(self) -> State: ...

    def attempt(self, table: TableKey, rows: list[RowKey]) -> bool: ...


def reduce(backend: Backend, progress: Callable[[str], None] = print) -> State:
    """Repeat passes until no FK-closed single-row removal preserves failure."""
    state = backend.state()
    changed = True
    while changed:
        changed = False
        for table in sorted(state):
            for size in sizes(len(state[table])):
                present = set(state[table])
                for candidate in chunks(list(state[table]), size):
                    # Earlier FK cascades may have removed rows from this chunk.
                    candidate = [row for row in candidate if row in present]
                    if candidate and backend.attempt(table, candidate):
                        before = sum(map(len, state.values()))
                        state = backend.state()
                        present = set(state[table])
                        after = sum(map(len, state.values()))
                        if after >= before:
                            raise RuntimeError("Accepted candidate did not reduce row count")
                        progress(f"{'.'.join(table)} (chunk {size}): {before} -> {after}")
                        changed = True
    return state
