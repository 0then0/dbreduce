from dataclasses import dataclass

TableKey = tuple[str, str]
RowKey = tuple[str, int]
State = dict[TableKey, list[RowKey]]


@dataclass(frozen=True)
class Table:
    key: TableKey
    primary_key: tuple[str, ...]
    rows: int

    @property
    def label(self) -> str:
        return ".".join(self.key)


@dataclass(frozen=True)
class ForeignKey:
    name: str
    child: TableKey
    parent: TableKey
    columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    operators: tuple[tuple[str, str], ...]
    collations: tuple[tuple[str, str] | None, ...] = ()
    virtual: bool = False


@dataclass(frozen=True)
class Schema:
    tables: tuple[Table, ...]
    foreign_keys: tuple[ForeignKey, ...]
