"""Opt-in integration tests create and drop only randomly named disposable databases."""

import os
import shlex
import shutil
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from dbreduce.cache.store import Cache
from dbreduce.oracle.runner import Oracle
from dbreduce.performance import Performance
from dbreduce.postgres.backend import CloneBackend, PostgresBackend
from dbreduce.postgres.clone import CloneStore, check_clone_source
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


def test_archive_with_newline_database_name_is_rejected_without_writes(tmp_path):
    from typer.testing import CliRunner

    from dbreduce.cli import app

    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin or not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore")):
        pytest.skip("Set DBREDUCE_TEST_ADMIN and install PostgreSQL client tools")
    name = f"dbreduce_{uuid.uuid4().hex}\npart"
    source_dsn = make_conninfo(admin, dbname=name)
    archive = tmp_path / "source.dump"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(source_dsn) as conn:
            conn.execute("CREATE TABLE source_data (id int)")
            conn.execute("INSERT INTO source_data VALUES (1)")
        dump(source_dsn, archive)
        with pytest.raises(ValueError, match="name containing a newline or carriage return"):
            read_archive_settings(archive)
        output = tmp_path / "result.sql"
        report = tmp_path / "report.json"
        result = CliRunner().invoke(
            app,
            [
                "reduce",
                "--dump",
                str(archive),
                "--admin-database",
                admin,
                "--oracle",
                "false",
                "--output",
                str(output),
                "--report",
                str(report),
            ],
        )
        assert result.exit_code == 1
        assert "database name containing a newline or carriage return" in result.output
        assert not output.exists() and not report.exists()
        with psycopg.connect(source_dsn) as conn:
            assert conn.execute("SELECT id FROM source_data").fetchall() == [(1,)]
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_lost_create_reply_leaves_unverified_database_for_inspection():
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    real_connect = psycopg.connect
    created_on_server = False
    created_identity = None

    class LostReplyConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, query, *args, **kwargs):
            nonlocal created_on_server
            result = self.connection.execute(query, *args, **kwargs)
            if isinstance(query, sql.Composed) and query.as_string().startswith("CREATE DATABASE"):
                created_on_server = True
                self.connection.close()
                raise psycopg.OperationalError("server reply lost")
            return result

    def connect_with_lost_reply(*args, **kwargs):
        return LostReplyConnection(real_connect(*args, **kwargs))

    workspace = Workspace(admin)
    try:
        with patch(
            "dbreduce.postgres.database.psycopg.connect", side_effect=connect_with_lost_reply
        ):
            with pytest.raises(RuntimeError, match="unverified"):
                with workspace:
                    workspace.create()
        assert created_on_server
        assert not workspace.created
        with real_connect(admin) as conn:
            created_identity = conn.execute(
                "SELECT oid, datdba FROM pg_database WHERE datname = %s",
                (workspace.name,),
            ).fetchone()
            assert created_identity is not None
    finally:
        if created_on_server:
            with real_connect(admin, autocommit=True) as conn:
                current = conn.execute(
                    "SELECT oid, datdba FROM pg_database WHERE datname = %s",
                    (workspace.name,),
                ).fetchone()
                if current is not None and created_identity == current:
                    conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(workspace.name)))


def test_workspace_does_not_drop_same_name_replacement():
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    workspace = Workspace(admin)
    real_connect = psycopg.connect
    replacement_oid = None
    try:
        workspace.create()
        owned_oid = workspace.oid
        with real_connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(workspace.name)))
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(workspace.name)
                )
            )
            replacement_oid = conn.execute(
                "SELECT oid FROM pg_database WHERE datname = %s", (workspace.name,)
            ).fetchone()[0]
        assert replacement_oid != owned_oid
        with pytest.raises(RuntimeError, match="Ownership.*cannot be verified"):
            workspace.close()
        with real_connect(admin) as conn:
            assert conn.execute(
                "SELECT oid FROM pg_database WHERE datname = %s", (workspace.name,)
            ).fetchone()[0] == replacement_oid
    finally:
        if replacement_oid is not None:
            with real_connect(admin, autocommit=True) as conn:
                current = conn.execute(
                    "SELECT oid FROM pg_database WHERE datname = %s", (workspace.name,)
                ).fetchone()
                if current is not None and current[0] == replacement_oid:
                    conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(workspace.name)))


def test_disconnect_while_create_is_waiting_reports_unverified_database():
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    real_connect = psycopg.connect
    creator = real_connect(admin, autocommit=True)
    blocker = real_connect(admin, autocommit=True)
    creator.execute("SET lock_timeout = '120s'")
    creator_pid = creator.info.backend_pid
    workspace = Workspace(admin)
    errors = []
    connections = 0
    cleanup_connecting = threading.Event()
    thread = None
    name_was_absent = False
    observed_identity = None

    class CreatorConnection:
        def __enter__(self):
            creator.__enter__()
            return self

        def __exit__(self, *args):
            return creator.__exit__(*args)

        def execute(self, query, *args, **kwargs):
            return creator.execute(query, *args, **kwargs)

    def connect_for_workspace(*args, **kwargs):
        nonlocal connections
        connections += 1
        if connections == 1:
            return CreatorConnection()
        cleanup_connecting.set()
        return real_connect(*args, **kwargs)

    def run_workspace():
        try:
            with workspace:
                workspace.create()
        except Exception as error:
            errors.append(error)

    try:
        name_was_absent = not creator.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
            (workspace.name,),
        ).fetchone()[0]
        assert name_was_absent
        blocker.execute("BEGIN")
        try:
            blocker.execute("LOCK TABLE pg_catalog.pg_database IN ACCESS EXCLUSIVE MODE")
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("Locking pg_database requires superuser access")
        with patch(
            "dbreduce.postgres.database.psycopg.connect",
            side_effect=connect_for_workspace,
        ):
            thread = threading.Thread(target=run_workspace, daemon=True)
            thread.start()
            deadline = time.monotonic() + 30
            blocked = False
            while time.monotonic() < deadline:
                blocked = blocker.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                    (creator.info.backend_pid,),
                ).fetchone()[0]
                if blocked or not thread.is_alive():
                    break
                time.sleep(0.05)
            assert blocked and thread.is_alive(), errors

            with socket.socket(fileno=os.dup(creator.pgconn.socket)) as connection_socket:
                connection_socket.shutdown(socket.SHUT_RDWR)
            assert cleanup_connecting.wait(timeout=30)
            blocker.execute("ROLLBACK")
            thread.join(timeout=30)
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], RuntimeError)
        assert workspace.name in str(errors[0])
        assert not workspace.created
        with real_connect(admin) as observer:
            observed_identity = observer.execute(
                "SELECT oid, datdba FROM pg_database WHERE datname = %s",
                (workspace.name,),
            ).fetchone()
    finally:
        try:
            blocker.execute("ROLLBACK")
        except psycopg.Error:
            pass
        blocker.close()
        if thread is not None:
            thread.join(timeout=30)
        if name_was_absent:
            try:
                with real_connect(admin, autocommit=True, connect_timeout=10) as cleanup:
                    cleanup.execute("SET statement_timeout = '10s'")
                    active_create = (
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE pid = %s AND state = 'active' AND query LIKE %s)"
                    )
                    create_query = f'CREATE DATABASE "{workspace.name}"%'
                    cleanup.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE pid = %s AND state = 'active' AND query LIKE %s",
                        (creator_pid, create_query),
                    )
                    deadline = time.monotonic() + 30
                    while cleanup.execute(active_create, (creator_pid, create_query)).fetchone()[0]:
                        assert time.monotonic() < deadline, "CREATE backend did not terminate"
                        time.sleep(0.05)
                    current = cleanup.execute(
                        "SELECT oid, datdba FROM pg_database WHERE datname = %s",
                        (workspace.name,),
                    ).fetchone()
                    if observed_identity is not None and current == observed_identity:
                        cleanup.execute(
                            sql.SQL("DROP DATABASE {}").format(sql.Identifier(workspace.name))
                        )
            finally:
                creator.close()
        else:
            creator.close()
        if thread is not None:
            thread.join(timeout=30)
        if thread is not None and thread.is_alive():
            pytest.fail("CREATE worker did not finish after releasing the catalog lock")


def test_workspace_cleanup_can_retry_after_connection_failure():
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin:
        pytest.skip("Set DBREDUCE_TEST_ADMIN")
    workspace = Workspace(admin)
    created_on_server = False
    try:
        workspace.create()
        created_on_server = True
        with patch(
            "dbreduce.postgres.database.psycopg.connect",
            side_effect=psycopg.OperationalError("server unavailable"),
        ):
            with pytest.raises(RuntimeError, match=workspace.name) as error:
                workspace.close()
        assert isinstance(error.value.__cause__, psycopg.OperationalError)
        assert workspace.created
        with psycopg.connect(admin) as conn:
            assert conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (workspace.name,),
            ).fetchone()[0]
        workspace.close()
        assert not workspace.created
        with psycopg.connect(admin) as conn:
            assert not conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (workspace.name,),
            ).fetchone()[0]
    finally:
        if created_on_server:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(workspace.name)
                    )
                )


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
    for table, rows in final.items():
        for row in rows:
            assert not backend.attempt(table, [row])
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


@pytest.mark.parametrize("restore_jobs", [1, 4])
def test_trigger_rejection_does_not_stop_reduction(workspace, tmp_path, restore_jobs):
    workspace.restore_jobs = restore_jobs
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
    assert backend.raise_rejections >= 1
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
    report_data = json.loads(report.read_text())
    assert report_data["final_rows"] == 2
    assert report_data["raise_exception_rejections"] == 0
    dump(workspace.dsn, after, archive=False, restrict_key="TestSourceUnchanged")
    assert before.read_bytes() == after.read_bytes()


def test_cli_does_not_publish_when_workspace_cleanup_fails(workspace, tmp_path):
    from typer.testing import CliRunner

    from dbreduce.cli import app

    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("DELETE FROM child WHERE id > 2")
        conn.execute("DELETE FROM parent WHERE id > 2")
    code = (
        "import os,sys,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "sys.exit(int(c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=1)').fetchone()[0]))"
    )
    output = tmp_path / "min.sql"
    report = tmp_path / "report.json"
    copies = []

    def fail_cleanup(copy, *_args):
        copies.append(copy)
        raise RuntimeError(f"Could not confirm cleanup of workspace database {copy.name}")

    try:
        with patch.object(Workspace, "__exit__", fail_cleanup):
            with patch("dbreduce.cli.dump", wraps=dump) as dump_call:
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
        assert any(call.kwargs.get("create_database") for call in dump_call.call_args_list)
        assert result.exit_code == 1
        assert copies and copies[0].name in result.output
        assert not output.exists() and not report.exists()
    finally:
        for copy in copies:
            copy.close()


def test_cli_inspect_reports_fk_graph_without_writes(workspace):
    from typer.testing import CliRunner

    from dbreduce.cli import app

    result = CliRunner().invoke(app, ["inspect", "--database", workspace.dsn])
    assert result.exit_code == 0, result.output
    assert "public.child" in result.output
    assert "public.child -> public.parent" in result.output
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 20


@pytest.mark.parametrize("composite", [False, True])
def test_virtual_closure(workspace, tmp_path, composite):
    import json

    from dbreduce.postgres.relationships import load_relationships

    config = tmp_path / "relationships.json"
    columns = ["tenant", "id"] if composite else ["id"]
    config.write_text(
        json.dumps(
            {
                "relationships": [
                    {
                        "from": {"table": "semantic_child", "columns": columns},
                        "to": {"table": "semantic_parent", "columns": columns},
                    }
                ]
            }
        )
    )
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE TABLE semantic_parent (tenant int, id int)")
        conn.execute("CREATE TABLE semantic_child (tenant int, id int)")
        conn.execute("INSERT INTO semantic_parent VALUES (1, 1), (2, 1)")
        conn.execute("INSERT INTO semantic_child VALUES (1, 1), (2, 1), (1, NULL)")
        schema = load_relationships(conn, inspect_database(conn), config)
        state, _ = read_rows(conn, schema)
        delete_rows(
            conn, schema, ("public", "semantic_parent"), state[("public", "semantic_parent")][:1]
        )
        assert conn.execute("SELECT count(*) FROM semantic_child").fetchone()[0] == (
            2 if composite else 1
        )


def test_different_failure_rejected_with_oracle_writes(workspace, tmp_path):
    snapshot = tmp_path / "accepted.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    code = (
        "import os,json,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "bug=c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=7)').fetchone()[0]; "
        "c.execute('INSERT INTO parent VALUES (999)'); c.commit(); "
        "print(json.dumps({'reproduced': True, 'signature': 'BUG_A' if bug else 'BUG_B'}))"
    )
    oracle = Oracle(
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", structured=True, confirm=2
    )
    assert oracle.fails(workspace.url, lambda: workspace.reset(snapshot))
    workspace.reset(snapshot)
    backend = PostgresBackend(workspace, schema, snapshot, oracle, Cache())
    final = reduce(backend, lambda _: None)
    assert {key: len(rows) for key, rows in final.items()} == {
        ("public", "parent"): 1,
        ("public", "child"): 1,
    }
    for table, rows in final.items():
        for row in rows:
            assert not backend.attempt(table, [row])
    assert oracle.outcomes["different_failure"] > 0
    assert oracle.fails(workspace.url, lambda: workspace.reset(snapshot))
    workspace.reset(snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM parent").fetchall() == [(7,)]


def test_clone_rejects_different_failure_and_isolates_oracle_writes(workspace, tmp_path):
    snapshot = tmp_path / "accepted.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
        initial, _ = read_rows(conn, schema)
    code = (
        "import os,json,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "bug=c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=7)').fetchone()[0]; "
        "c.execute('INSERT INTO parent VALUES (999)'); c.commit(); "
        "print(json.dumps({'reproduced': True, 'signature': 'BUG_A' if bug else 'BUG_B'}))"
    )
    oracle = Oracle(
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", structured=True, confirm=2
    )
    assert oracle.fails(workspace.url, lambda: workspace.reset(snapshot))
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    final_dump = tmp_path / "final.dump"
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        accepted, accepted_state = store.create_initial(snapshot, schema)
        backend = CloneBackend(store, schema, accepted, accepted_state, oracle, Performance())
        assert backend.confirm_initial_state()
        accepted_copy = store.clone(accepted)
        try:
            with psycopg.connect(store.dsn(accepted_copy)) as conn:
                assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 20
        finally:
            store.drop(accepted_copy)
        final = reduce(backend, lambda _: None)
        assert {key: len(rows) for key, rows in final.items()} == {
            ("public", "parent"): 1,
            ("public", "child"): 1,
        }
        for table, rows in final.items():
            for row in rows:
                assert not backend.attempt(table, [row])
        assert oracle.outcomes["different_failure"] > 0
        copy = store.clone(backend.accepted_db)
        try:
            with psycopg.connect(store.dsn(copy)) as conn:
                assert conn.execute("SELECT id FROM parent").fetchall() == [(7,)]
            dump(store.dsn(copy), final_dump)
        finally:
            store.drop(copy)
    workspace.reset(final_dump)
    assert oracle.fails(workspace.url, lambda: workspace.reset(final_dump))
    workspace.reset(final_dump)
    with psycopg.connect(workspace.dsn) as conn:
        assert conn.execute("SELECT id FROM parent").fetchall() == [(7,)]


def test_clone_initial_state_is_read_from_restored_accepted_database(workspace, tmp_path):
    snapshot = tmp_path / "accepted.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
        conn.execute("INSERT INTO parent VALUES (999)")
    with CloneStore(
        os.environ["DBREDUCE_TEST_ADMIN"], read_settings(workspace.dsn), Performance()
    ) as store:
        accepted, initial_state = store.create_initial(snapshot, schema)
        assert len(initial_state[("public", "parent")]) == 20
        copy = store.clone(accepted)
        try:
            with psycopg.connect(store.dsn(copy)) as conn:
                actual_state, _ = read_rows(conn, schema)
            assert initial_state == actual_state
        finally:
            store.drop(copy)


def test_clone_ownership_marker_blocks_unverified_cleanup(workspace, tmp_path):
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        accepted, _ = store.create_initial(snapshot, schema)
        candidate = store.clone(accepted)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("COMMENT ON DATABASE {} IS 'changed'").format(
                    sql.Identifier(candidate.name)
                )
            )
        with pytest.raises(RuntimeError, match="Ownership.*cannot be verified"):
            store.drop(candidate)
        with psycopg.connect(admin) as conn:
            assert conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (candidate.name,),
            ).fetchone()[0]
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(candidate.name), sql.Literal(store.marker)
                )
            )
        store.drop(candidate)


def test_clone_rejected_create_error_is_not_reported_as_ambiguous(workspace):
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        with pytest.raises(psycopg.errors.InvalidCatalogName):
            store._create(
                lambda name: sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(name), sql.Identifier("dbreduce_missing_template")
                )
            )
        assert store.unproven == set()


def test_clone_freeze_rejects_unexpected_session_without_termination(workspace, tmp_path):
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        accepted, _ = store.create_initial(snapshot, schema)
        candidate = store.clone(accepted)
        with psycopg.connect(store.dsn(candidate)) as unexpected:
            with pytest.raises(RuntimeError, match="unexpected sessions"):
                store.freeze(candidate)
            assert unexpected.execute("SELECT 1").fetchone()[0] == 1
        store.freeze(candidate)
        store.drop(candidate)


def test_clone_promotion_keeps_exact_trigger_result(workspace, tmp_path):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("DELETE FROM child WHERE id > 2")
        conn.execute("DELETE FROM parent WHERE id > 2")
        conn.execute("CREATE TABLE marker (id int PRIMARY KEY, value uuid NOT NULL)")
        conn.execute("INSERT INTO marker VALUES (1, gen_random_uuid())")
        conn.execute("""
            CREATE FUNCTION mark_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE marker SET value = gen_random_uuid() WHERE id = 1;
                RETURN OLD;
            END $$
        """)
        conn.execute("""
            CREATE TRIGGER mark BEFORE DELETE ON child
            FOR EACH ROW EXECUTE FUNCTION mark_deletion()
        """)
        schema = inspect_database(conn)
        initial, _ = read_rows(conn, schema)
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    code = (
        "import os,json,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "bug=c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=1)').fetchone()[0]; "
        "print(json.dumps({'reproduced':bug,'signature':'BUG_A' if bug else None}))"
    )
    oracle = Oracle(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", structured=True)
    assert oracle.fails(workspace.url, lambda: workspace.reset(snapshot))
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        accepted, accepted_state = store.create_initial(snapshot, schema)
        backend = CloneBackend(store, schema, accepted, accepted_state, oracle, Performance())
        assert backend.attempt(("public", "child"), initial[("public", "child")][1:])
        expected = backend.state()[("public", "marker")]
        assert expected != initial[("public", "marker")]
        copy = store.clone(backend.accepted_db)
        try:
            with psycopg.connect(store.dsn(copy)) as conn:
                actual, _ = read_rows(conn, schema)
            assert actual[("public", "marker")] == expected
        finally:
            store.drop(copy)


def test_clone_preflight_rejects_publications(workspace, tmp_path):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("CREATE PUBLICATION dbreduce_test_publication FOR TABLE parent")
        schema = inspect_database(conn)
        with pytest.raises(ValueError, match="publications"):
            check_clone_source(conn)
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        with pytest.raises(ValueError, match="publications"):
            store.create_initial(snapshot, schema)


@pytest.mark.parametrize(
    "phase", ["candidate_clone", "deletion", "oracle_clone", "oracle", "promotion"]
)
def test_clone_interrupt_cleans_only_owned_databases(workspace, tmp_path, phase):
    with psycopg.connect(workspace.dsn) as conn:
        conn.execute("DELETE FROM child WHERE id > 2")
        conn.execute("DELETE FROM parent WHERE id > 2")
        schema = inspect_database(conn)
        initial, _ = read_rows(conn, schema)
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    code = (
        "import os,json,psycopg; "
        "c=psycopg.connect(os.environ['DATABASE_URL']); "
        "bug=c.execute('SELECT EXISTS(SELECT 1 FROM child WHERE id=1)').fetchone()[0]; "
        "print(json.dumps({'reproduced':bug,'signature':'BUG_A' if bug else None}))"
    )
    oracle = Oracle(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", structured=True)
    assert oracle.fails(workspace.url, lambda: workspace.reset(snapshot))
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with pytest.raises(KeyboardInterrupt):
        with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
            accepted, accepted_state = store.create_initial(snapshot, schema)
            backend = CloneBackend(
                store, schema, accepted, accepted_state, oracle, Performance()
            )
            if phase in ("candidate_clone", "oracle_clone"):
                original_clone = store.clone
                clone_count = 0

                def interrupted_clone(source):
                    nonlocal clone_count
                    clone_count += 1
                    created = original_clone(source)
                    if phase == "candidate_clone" or clone_count == 2:
                        raise KeyboardInterrupt
                    return created

                with patch.object(store, "clone", side_effect=interrupted_clone):
                    backend.attempt(("public", "child"), initial[("public", "child")][1:])
            elif phase == "deletion":
                with patch("dbreduce.postgres.backend.delete_rows", side_effect=KeyboardInterrupt):
                    backend.attempt(("public", "child"), initial[("public", "child")][1:])
            elif phase == "oracle":
                with patch.object(oracle, "_run", side_effect=KeyboardInterrupt):
                    backend.attempt(("public", "child"), initial[("public", "child")][1:])
            else:
                original_drop = store.drop

                def interrupt_promotion(database):
                    if database is accepted:
                        raise KeyboardInterrupt
                    original_drop(database)

                with patch.object(store, "drop", side_effect=interrupt_promotion):
                    backend.attempt(("public", "child"), initial[("public", "child")][1:])
    with psycopg.connect(admin) as conn:
        assert not conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database "
            "WHERE shobj_description(oid, 'pg_database') = %s)",
            (store.marker,),
        ).fetchone()[0]


def test_clone_cleanup_interrupt_names_owned_leftover(workspace, tmp_path):
    snapshot = tmp_path / "source.dump"
    dump(workspace.dsn, snapshot)
    with psycopg.connect(workspace.dsn) as conn:
        schema = inspect_database(conn)
    admin = os.environ["DBREDUCE_TEST_ADMIN"]
    with CloneStore(admin, read_settings(workspace.dsn), Performance()) as store:
        accepted, _ = store.create_initial(snapshot, schema)
        with patch.object(store, "drop", side_effect=KeyboardInterrupt):
            with pytest.raises(RuntimeError, match=accepted.name + ".*KeyboardInterrupt"):
                store.close()
        with psycopg.connect(admin) as conn:
            assert conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE oid = %s)",
                (accepted.oid,),
            ).fetchone()[0]
