from unittest.mock import MagicMock, patch

from dbreduce.cache.store import Cache
from dbreduce.models.schema import Schema
from dbreduce.postgres.backend import PostgresBackend


def test_cache_distinguishes_sequence_changes_with_identical_rows(tmp_path):
    table = ("public", "items")
    baseline = {table: [("a", 0), ("b", 0)]}
    candidate = {table: [("a", 0)]}
    connection = MagicMock()
    connection.__enter__.return_value = connection
    workspace = MagicMock()
    oracle = MagicMock()
    oracle.fails.return_value = False
    snapshot = tmp_path / "accepted.dump"
    snapshot.write_bytes(b"accepted")
    sql_bytes = [b"same data, sequence 1"]

    def fake_dump(dsn, path, **kwargs):
        path.write_bytes(sql_bytes[0])

    with (
        patch("dbreduce.postgres.backend.psycopg.connect", return_value=connection),
        patch("dbreduce.postgres.backend.read_rows", return_value=(baseline, {})) as read,
        patch("dbreduce.postgres.backend.delete_rows"),
        patch("dbreduce.postgres.backend.dump", side_effect=fake_dump),
    ):
        cache = Cache()
        backend = PostgresBackend(workspace, Schema((), ()), snapshot, oracle, cache)
        read.return_value = (candidate, {})
        assert not backend.attempt(table, [("b", 0)])
        assert not backend.attempt(table, [("b", 0)])
        assert oracle.fails.call_count == 1
        assert cache.hits == 1
        sql_bytes[0] = b"same data, sequence 2"
        assert not backend.attempt(table, [("b", 0)])
        assert oracle.fails.call_count == 2
        assert snapshot.read_bytes() == b"accepted"
        assert backend.state() == baseline


def test_candidate_rejected_if_restore_returns_deleted_rows(tmp_path):
    table = ("public", "extension_seed")
    baseline = {table: [('{"id":1}', 0)]}
    connection = MagicMock()
    connection.__enter__.return_value = connection
    workspace = MagicMock()
    oracle = MagicMock()
    snapshot = tmp_path / "accepted.dump"
    snapshot.write_bytes(b"original")

    def fake_dump(dsn, path, **kwargs):
        path.write_bytes(b"archive recreates seed row")

    with (
        patch("dbreduce.postgres.backend.psycopg.connect", return_value=connection),
        patch("dbreduce.postgres.backend.read_rows", return_value=(baseline, {})),
        patch("dbreduce.postgres.backend.delete_rows"),
        patch("dbreduce.postgres.backend.dump", side_effect=fake_dump),
    ):
        backend = PostgresBackend(workspace, Schema((), ()), snapshot, oracle, Cache())
        assert not backend.attempt(table, baseline[table])
    assert backend.state() == baseline
    assert snapshot.read_bytes() == b"original"
    oracle.fails.assert_not_called()
