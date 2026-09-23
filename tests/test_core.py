from dbreduce.cache.store import Cache, fingerprint
from dbreduce.graph.dependencies import components
from dbreduce.models.schema import ForeignKey, Schema, Table
from dbreduce.reducer.ddmin import sizes
from dbreduce.reducer.engine import reduce


def test_fingerprint_includes_schema_and_sequences(tmp_path):
    snapshot = tmp_path / "state.sql"
    snapshot.write_text("CREATE TABLE a (id int); SELECT setval('seq', 1);")
    first = fingerprint(snapshot)
    assert fingerprint(snapshot) == first
    snapshot.write_text("CREATE TABLE a (id int); SELECT setval('seq', 2);")
    assert fingerprint(snapshot) != first


def test_fingerprint_ignores_copy_row_order_but_counts_duplicates(tmp_path):
    snapshot = tmp_path / "state.sql"
    header = b"CREATE TABLE t (id int);\nCOPY public.t (id) FROM stdin;\n"
    snapshot.write_bytes(header + b"1\n2\n\\.\n")
    first = fingerprint(snapshot)
    snapshot.write_bytes(header + b"2\n1\n\\.\n")
    assert fingerprint(snapshot) == first
    snapshot.write_bytes(header + b"2\n1\n1\n\\.\n")
    assert fingerprint(snapshot) != first


def test_cache_retains_negative_results():
    cache = Cache()
    cache.results["x"] = False
    assert cache.get("x") is False
    assert cache.get("unknown") is None
    assert cache.hits == 1


def test_sizes_terminate_at_one():
    assert list(sizes(0)) == []
    assert list(sizes(7)) == [7, 4, 2, 1]


def test_cycles_and_self_references():
    a, b, c = ("s", "a"), ("s", "b"), ("s", "c")
    schema = Schema(
        tuple(Table(key, (), 0) for key in (a, b, c)),
        tuple(
            ForeignKey("fk", child, parent, ("id",), ("id",), (("pg_catalog", "="),))
            for child, parent in ((a, b), (b, a), (c, c))
        ),
    )
    assert components(schema) == [(a, b), (c,)]


def test_reducer_keeps_required_combination():
    class MemoryBackend:
        def __init__(self):
            self.rows = {("public", "items"): [(str(i), 0) for i in range(40)]}

        def state(self):
            return self.rows

        def attempt(self, table, rows):
            remaining = [row for row in self.rows[table] if row not in rows]
            if not {("7", 0), ("29", 0)} <= set(remaining):
                return False
            self.rows = {table: remaining}
            return True

    result = reduce(MemoryBackend(), lambda _: None)
    assert result == {("public", "items"): [("7", 0), ("29", 0)]}
