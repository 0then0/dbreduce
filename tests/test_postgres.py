"""Opt-in integration tests create and drop only randomly named disposable databases."""

import os
import shlex
import shutil
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from dbreduce.cache.store import Cache
from dbreduce.oracle.runner import Oracle
from dbreduce.postgres.backend import PostgresBackend
from dbreduce.postgres.database import (
    DatabaseSettings,
    Workspace,
    database_url,
    read_archive_settings,
    read_settings,
)
from dbreduce.postgres.dump import client, dump
from dbreduce.postgres.introspection import check_extension_tables, inspect_database
from dbreduce.postgres.rows import delete_rows, read_rows
from dbreduce.reducer.engine import reduce

pytestmark = pytest.mark.postgres


@pytest.fixture
def workspace(tmp_path):
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin or not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore", "psql")):
        pytest.skip("Set DBREDUCE_TEST_ADMIN and install PostgreSQL client tools")
    initial = tmp_path / "fixture.sql"
    initial.write_text("""
        CREATE TABLE parent (id integer PRIMARY KEY);
        CREATE TABLE child (id integer PRIMARY KEY, parent_id integer REFERENCES parent);
        INSERT INTO parent SELECT generate_series(1, 20);
        INSERT INTO child SELECT i, i FROM generate_series(1, 20) AS i;
    """)
    with Workspace(admin) as workspace:
        workspace.create()
        with psycopg.connect(workspace.dsn) as conn:
            conn.execute(initial.read_text())
        yield workspace


def test_fk_closure(workspace):
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        delete_rows(conn, schema, ("public", "parent"), state[("public", "parent")][:1])
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 19
        assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 19


@pytest.mark.parametrize("action", ["CASCADE", "SET NULL"])
def test_fk_closure_with_delete_action(workspace, action):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE TABLE action_parent (id int PRIMARY KEY)")
        conn.execute(
            f"CREATE TABLE action_child (id int PRIMARY KEY, "
            f"parent_id int REFERENCES action_parent ON DELETE {action})"
        )
        conn.execute("INSERT INTO action_parent VALUES (1), (2)")
        conn.execute("INSERT INTO action_child VALUES (1, 1), (2, 2)")
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        delete_rows(
            conn, schema, ("public", "action_parent"), state[("public", "action_parent")][:1]
        )
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM action_parent ORDER BY id").fetchall() == [(2,)]
        assert conn.execute("SELECT id, parent_id FROM action_child ORDER BY id").fetchall() == [
            (2, 2)
        ]


def test_nondeferrable_cycle(workspace):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE TABLE a (id int PRIMARY KEY, b_id int)")
        conn.execute("CREATE TABLE b (id int PRIMARY KEY, a_id int REFERENCES a)")
        conn.execute("INSERT INTO a VALUES (1, 1)")
        conn.execute("INSERT INTO b VALUES (1, 1)")
        conn.execute("ALTER TABLE a ADD FOREIGN KEY (b_id) REFERENCES b")
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        delete_rows(conn, schema, ("public", "a"), state[("public", "a")])
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM a").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM b").fetchone()[0] == 0


def test_fk_with_different_deterministic_collations(workspace):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute('CREATE TABLE text_parent (id text COLLATE "C" PRIMARY KEY)')
        conn.execute(
            "CREATE TABLE text_child (id int PRIMARY KEY, "
            'parent_id text COLLATE "POSIX" REFERENCES text_parent)'
        )
        conn.execute("INSERT INTO text_parent VALUES ('x')")
        conn.execute("INSERT INTO text_child VALUES (1, 'x'), (2, NULL)")
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        delete_rows(conn, schema, ("public", "text_parent"), state[("public", "text_parent")])
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM text_child").fetchone()[0] == 1


def test_composite_fk_with_nullable_child(workspace):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE TABLE paired_parent (a int, b int, PRIMARY KEY (a, b))")
        conn.execute(
            "CREATE TABLE paired_child (id int PRIMARY KEY, x int, y int, "
            "FOREIGN KEY (x, y) REFERENCES paired_parent(a, b))"
        )
        conn.execute("INSERT INTO paired_parent VALUES (1, 2)")
        conn.execute("INSERT INTO paired_child VALUES (1, 1, 2), (2, NULL, 2)")
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        delete_rows(conn, schema, ("public", "paired_parent"), state[("public", "paired_parent")])
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM paired_child").fetchall() == [(2,)]


def test_duplicate_rows_without_primary_key(workspace, tmp_path):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE TABLE duplicated (value text)")
        conn.execute("INSERT INTO duplicated VALUES ('x'), ('x')")
        schema = inspect_database(conn)
        state, _ = read_rows(conn, schema)
        assert len(state[("public", "duplicated")]) == 2
        delete_rows(conn, schema, ("public", "duplicated"), state[("public", "duplicated")][:1])
    snapshot = tmp_path / "one-duplicate.dump"
    dump(workspace.dsn, snapshot)
    workspace.reset(snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM duplicated").fetchone()[0] == 1


def test_extension_owned_table_is_rejected(workspace):
    with psycopg.connect(workspace.dsn) as conn:
        available = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='hstore')"
        ).fetchone()[0]
        if not available:
            pytest.skip("hstore extension not installed")
        conn.execute("CREATE EXTENSION hstore")
        conn.execute("CREATE TABLE extension_seed (id int PRIMARY KEY)")
        conn.execute("INSERT INTO extension_seed VALUES (1)")
        conn.execute("ALTER EXTENSION hstore ADD TABLE extension_seed")
        assert ("public", "extension_seed") in {
            table.key for table in inspect_database(conn).tables
        }
        with pytest.raises(ValueError, match="extension-owned table"):
            check_extension_tables(conn)
        assert conn.execute("SELECT count(*) FROM extension_seed").fetchone()[0] == 1


@pytest.mark.parametrize("provider", ["c", "i"])
def test_source_encoding_and_locale_are_copied(provider):
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    if provider == "i":
        with psycopg.connect(admin) as conn:
            if not conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_collation WHERE collprovider='i')"
            ).fetchone()[0]:
                pytest.skip("PostgreSQL was built without ICU")
        admin_settings = read_settings(admin)
        source_settings = DatabaseSettings(
            admin_settings.encoding,
            admin_settings.lc_collate,
            admin_settings.lc_ctype,
            "i",
            "und",
            None,
        )
    else:
        source_settings = DatabaseSettings("UTF8", "C", "C", "c", None, None)
    with Workspace(admin, settings=source_settings) as source:
        source.create()
        discovered = read_settings(source.dsn)
        assert discovered == source_settings
        with Workspace(admin, settings=discovered) as copy:
            copy.create()
            assert read_settings(copy.dsn) == discovered


def test_archive_reduction_uses_archived_locale(tmp_path):
    import json

    from typer.testing import CliRunner

    from dbreduce.cli import app

    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    source_settings = DatabaseSettings("UTF8", "C", "C", "c", None, None)
    if read_settings(admin).lc_collate == source_settings.lc_collate:
        pytest.skip("This regression requires a cluster default locale different from C")
    with Workspace(admin, settings=source_settings) as source:
        source.create()
        with psycopg.connect(source.dsn) as conn:
            conn.execute("CREATE TABLE items (id int PRIMARY KEY)")
            conn.execute("INSERT INTO items VALUES (1), (2)")
        archive = tmp_path / "source.dump"
        dump(source.dsn, archive)
        discovered = read_archive_settings(archive)
        assert discovered.lc_collate == discovered.lc_ctype == "C"
        code = (
            "import os,sys,psycopg; "
            "c=psycopg.connect(os.environ['DATABASE_URL']); "
            "bug=c.execute(\"SELECT datcollate='C' AND "
            "EXISTS(SELECT 1 FROM items WHERE id=1) FROM pg_database "
            'WHERE datname=current_database()").fetchone()[0]; '
            "sys.exit(int(bug))"
        )
        result = CliRunner().invoke(
            app,
            [
                "reduce",
                "--dump",
                str(archive),
                "--admin-database",
                admin,
                "--oracle",
                f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                "--output",
                str(tmp_path / "min.sql"),
                "--report",
                str(tmp_path / "report.json"),
            ],
        )
        assert result.exit_code == 0, result.output
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["final_rows"] == 1
        restored_name = report["restore_database"]
        assert restored_name.startswith("dbreduce_")
        try:
            client(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(tmp_path / "min.sql")], admin)
            restored_dsn = make_conninfo(admin, dbname=restored_name)
            assert read_settings(restored_dsn) == source_settings
            assert Oracle(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}").fails(
                database_url(admin, restored_name), lambda: None
            )
        finally:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(restored_name)
                    )
                )


def test_archive_settings_with_latin1_identifier(tmp_path):
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    with Workspace(admin, settings=DatabaseSettings("LATIN1", "C", "C", "c", None, None)) as source:
        source.create()
        with psycopg.connect(source.dsn) as conn:
            conn.execute('CREATE TABLE "café" (id int)')
        archive = tmp_path / "latin1.dump"
        dump(source.dsn, archive)
        settings = read_archive_settings(archive)
    assert settings.encoding == "LATIN1"
    assert settings.lc_collate == settings.lc_ctype == "C"


def test_reduction_and_oracle_writes_are_isolated(workspace, tmp_path):
    snapshot = tmp_path / "accepted.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    code = (
        "import os,sys,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "bug=c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=7)').fetchone()[0]; "
        "c.execute('INSERT INTO parent VALUES (999)'); c.commit(); sys.exit(int(bug))"
    )
    oracle = Oracle(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", confirm=2)
    backend = PostgresBackend(workspace, schema, snapshot, oracle, Cache())
    final = reduce(backend, lambda _: None)
    assert {key: len(rows) for key, rows in final.items()} == {
        ("public", "parent"): 1,
        ("public", "child"): 1,
    }
    workspace.reset(snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM parent").fetchall() == [(7,)]
    exported = tmp_path / "result.sql"
    dump(workspace.dsn, exported, archive=False)
    workspace.create()
    client(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-f", str(exported)], workspace.dsn)
    restored = tmp_path / "restored.dump"
    dump(workspace.dsn, restored)
    assert oracle.fails(workspace.url, lambda: workspace.reset(restored))


def test_trigger_rejection_does_not_stop_reduction(workspace, tmp_path):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("DELETE FROM child")
        conn.execute("DELETE FROM parent")
        conn.execute("CREATE TABLE guarded (id int PRIMARY KEY)")
        conn.execute("INSERT INTO guarded VALUES (1), (2)")
        conn.execute("""
            CREATE FUNCTION reject_guarded_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.id = 1 THEN RAISE EXCEPTION 'protected'; END IF;
                RETURN OLD;
            END $$
        """)
        conn.execute("""
            CREATE TRIGGER guard BEFORE DELETE ON guarded
            FOR EACH ROW EXECUTE FUNCTION reject_guarded_delete()
        """)
        schema = inspect_database(conn)
    snapshot = tmp_path / "accepted.dump"
    dump(workspace.dsn, snapshot)
    code = (
        "import os,sys,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "sys.exit(int(c.execute('SELECT EXISTS(SELECT 1 FROM guarded WHERE id=1)').fetchone()[0]))"
    )
    oracle = Oracle(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
    backend = PostgresBackend(workspace, schema, snapshot, oracle, Cache())

    final = reduce(backend, lambda _: None)

    assert len(final[("public", "guarded")]) == 1
    assert backend.constraint_rejections == 0
    workspace.reset(snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM guarded").fetchall() == [(1,)]


def test_demo_fixture_has_12005_rows(workspace):
    fixture = Path(__file__).parents[1] / "examples" / "fixture.sql"
    workspace.create()
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute(fixture.read_text())
    with psycopg.connect(workspace.dsn) as conn:
        assert sum(table.rows for table in inspect_database(conn).tables) == 12005


def test_cli_preserves_source_and_exports(workspace, tmp_path):
    import json

    from typer.testing import CliRunner

    from dbreduce.cli import app

    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("DELETE FROM child WHERE id > 2")
        conn.execute("DELETE FROM parent WHERE id > 2")
    before = tmp_path / "before.sql"
    after = tmp_path / "after.sql"
    dump(workspace.dsn, before, archive=False, restrict_key="TestSourceUnchanged")
    output = tmp_path / "min.sql"
    report = tmp_path / "report.json"
    code = (
        "import os,sys,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "sys.exit(int(c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=1)').fetchone()[0]))"
    )
    result = CliRunner().invoke(
        app,
        [
            "reduce",
            "--database",
            workspace.dsn,
            "--oracle",
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    assert json.loads(report.read_text())["final_rows"] == 2
    dump(workspace.dsn, after, archive=False, restrict_key="TestSourceUnchanged")
    assert before.read_bytes() == after.read_bytes()


def test_cli_inspect_reports_fk_graph_without_writes(workspace):
    from typer.testing import CliRunner

    from dbreduce.cli import app

    result = CliRunner().invoke(app, ["inspect", "--database", workspace.dsn])
    assert result.exit_code == 0, result.output
    assert "public.child" in result.output
    assert "public.child -> public.parent" in result.output
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 20
