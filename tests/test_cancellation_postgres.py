"""Cancellation of a real ``dbreduce reduce`` process against a disposable PostgreSQL.

Every test starts the CLI in its own process and signals it only after an observed stage:
the oracle (or a shimmed ``pg_restore``) writes a handshake once it is running, or the
server shows the expected session. The tests send signals straight to the DBReduce process
or to its process group; they do not reproduce ``docker stop`` or a CI runner's cancel.

Test cleanup uses raw SQL on names the test itself recorded or observed during the test, so it
never depends on the DBReduce code under test.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dbreduce.postgres.database import database_url

pytestmark = pytest.mark.postgres

GENERATED = re.compile(r"^dbreduce_[0-9a-f]{32}$")
EXTERNAL_TIMEOUT = 90  # seconds a DBReduce process may take before the test kills it
IS_LINUX = Path("/proc/self/environ").exists()

LAUNCHER = r"""
import os, sys, time
from pathlib import Path
import dbreduce.cancellation as cancellation

if "TEST_BUDGET" in os.environ:
    cancellation.SHUTDOWN_BUDGET = float(os.environ["TEST_BUDGET"])
    cancellation.REPORT_MARGIN = float(os.environ.get("TEST_MARGIN", "2"))

hook = os.environ.get("TEST_HOOK")
if hook:
    import psycopg
    from psycopg import sql

    original = psycopg.Connection.execute
    nth = int(os.environ.get("TEST_HOOK_NTH", "1"))
    seen = [0]

    def execute(self, query, *args, **kwargs):
        text = query.as_string(self) if isinstance(query, sql.Composable) else str(query)
        if text.startswith("CREATE DATABASE"):
            seen[0] += 1
            if seen[0] == nth:
                name = text.split()[2].strip('"')
                Path(os.environ["TEST_STATE"], "hook-database").write_text(name)
                if hook == "during_create":
                    # A statement that is still running on the server when the client stops.
                    return original(self, "SELECT pg_sleep(3600)")
                result = original(self, query, *args, **kwargs)
                time.sleep(3600)  # the database exists; the run marker was not written yet
                return result
        return original(self, query, *args, **kwargs)

    psycopg.Connection.execute = execute

from dbreduce.cli import app
app()
"""

ORACLE = r"""
import json, os, signal, sys, time
from pathlib import Path
import psycopg

state = Path(os.environ["TEST_STATE"])
with (state / "calls").open("a") as calls:
    calls.write("x\n")
count = len((state / "calls").read_text().split())
connection = psycopg.connect(os.environ["DATABASE_URL"])
if count == int(os.environ["TEST_BLOCK_ON"]):
    connection.execute("SELECT 1")  # a live session on the database under test
    mode = os.environ["TEST_ORACLE_MODE"]
    if mode == "term":
        def on_term(*_):
            (state / "term-seen").write_text("1")
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    else:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (state / "blocked.json").write_text(
        json.dumps({"pid": os.getpid(), "database": connection.info.dbname})
    )
    while True:
        time.sleep(1)
bug = connection.execute("SELECT EXISTS(SELECT 1 FROM child WHERE id = 1)").fetchone()[0]
print(json.dumps({"reproduced": bug, "signature": "BUG_A" if bug else None}))
"""

PG_RESTORE_SHIM = """#!/bin/sh
# Stands in for pg_restore: a child that keeps the output pipes open outlives its parent.
echo $$ > "$TEST_STATE/restore.pid"
sleep 600 &
echo $! > "$TEST_STATE/restore-child.pid"
echo started > "$TEST_STATE/restore.started"
wait
"""


def wait_for(predicate, timeout=60.0, interval=0.05, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"{what} not reached within {timeout} s")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # the process may vanish between the checks
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return not Path("/proc/self/stat").exists()  # no /proc at all: kill(0) already said alive


def marked_processes(run_id: str) -> list[int]:
    """Live processes that inherited this run's environment marker (all descendants)."""
    if not IS_LINUX:
        return []
    needle = f"TEST_RUN_ID={run_id}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if needle in (entry / "environ").read_bytes().split(b"\0") and process_alive(
                int(entry.name)
            ):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


class Server:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self, **kwargs):
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=10, **kwargs)

    def names(self) -> set[str]:
        with self.connect() as conn:
            return {row[0] for row in conn.execute("SELECT datname FROM pg_database")}

    def generated(self) -> set[str]:
        return {name for name in self.names() if GENERATED.match(name)}

    def comment(self, name: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
                (name,),
            ).fetchone()
        return None if row is None else row[0]

    def drop(self, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )

    def dsn_for(self, name: str) -> str:
        return make_conninfo(self.dsn, dbname=name)


@pytest.fixture
def env(tmp_path):
    if not IS_LINUX:
        pytest.skip("Process checks read /proc; run these tests on Linux")
    admin = os.environ.get("DBREDUCE_TEST_ADMIN")
    if not admin or not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore", "psql")):
        pytest.skip("Set DBREDUCE_TEST_ADMIN and install PostgreSQL client tools")
    server = Server(admin)
    suffix = uuid.uuid4().hex[:12]
    source, foreign = f"cancel_source_{suffix}", f"cancel_foreign_{suffix}"
    lookalike = f"dbreduce_{uuid.uuid4().hex}"  # looks generated but belongs to someone else
    for name in (source, foreign, lookalike):
        with server.connect() as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(server.dsn_for(source)) as conn:
        conn.execute("""
            CREATE TABLE parent (id integer PRIMARY KEY);
            CREATE TABLE child (id integer PRIMARY KEY, parent_id integer REFERENCES parent);
            INSERT INTO parent SELECT generate_series(1, 20);
            INSERT INTO child SELECT i, i FROM generate_series(1, 20) AS i;
        """)
    foreign_session = psycopg.connect(server.dsn_for(foreign), autocommit=True)
    foreign_session.execute("CREATE TABLE keep (id integer); INSERT INTO keep VALUES (1)")
    foreign_pid = foreign_session.info.backend_pid
    before = server.generated()
    assert lookalike in before
    context = SimpleNamespace(
        server=server,
        source=source,
        foreign=foreign,
        lookalike=lookalike,
        foreign_session=foreign_session,
        foreign_pid=foreign_pid,
        before=before,
        tmp=tmp_path,
        fingerprint=lambda: source_fingerprint(server.dsn_for(source)),
    )
    context.initial_fingerprint = context.fingerprint()
    try:
        yield context
    finally:
        # Independent of the code under test: drop only what appeared while this test ran.
        foreign_session.close()
        for name in server.generated() - before:
            server.drop(name)
        for name in (source, foreign, lookalike):
            server.drop(name)


def source_fingerprint(dsn: str):
    with psycopg.connect(dsn) as conn:
        return conn.execute("""
            SELECT (SELECT md5(string_agg(id::text, ',' ORDER BY id)) FROM parent),
                   (SELECT md5(string_agg(id || ':' || parent_id, ',' ORDER BY id)) FROM child),
                   (SELECT oid FROM pg_database WHERE datname = current_database())
        """).fetchone()


class Run:
    """One DBReduce process started for a test."""

    def __init__(
        self,
        env,
        *,
        dsn=None,
        backend="snapshot",
        block_on=2,
        oracle_mode="term",
        hook=None,
        hook_nth=1,
        budget=None,
        margin=None,
        path_prefix=None,
    ):
        self.env = env
        self.state = env.tmp / "state"
        self.state.mkdir()
        self.run_id = uuid.uuid4().hex
        self.cli_tmp = env.tmp / "cli-tmp"
        self.cli_tmp.mkdir()
        self.output, self.report = env.tmp / "min.sql", env.tmp / "report.json"
        (env.tmp / "oracle.py").write_text(ORACLE)
        admin = dsn or env.server.dsn
        source = make_conninfo(admin, dbname=env.source)
        process_env = {
            **os.environ,
            "TMPDIR": str(self.cli_tmp),
            "TEST_STATE": str(self.state),
            "TEST_RUN_ID": self.run_id,
            "TEST_BLOCK_ON": str(block_on),
            "TEST_ORACLE_MODE": oracle_mode,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if budget is not None:
            process_env["TEST_BUDGET"] = str(budget)
            process_env["TEST_MARGIN"] = str(margin if margin is not None else 2)
        if hook:
            process_env["TEST_HOOK"] = hook
            process_env["TEST_HOOK_NTH"] = str(hook_nth)
        if path_prefix:
            process_env["PATH"] = f"{path_prefix}{os.pathsep}{process_env['PATH']}"
        # exec: the oracle itself is the process in the group, not a shell that dies on SIGTERM.
        ignore = "trap '' TERM; " if oracle_mode == "ignore" else ""
        oracle = (
            f"{ignore}exec {shlex.quote(sys.executable)} {shlex.quote(str(env.tmp / 'oracle.py'))}"
        )
        command = [
            sys.executable, "-c", LAUNCHER, "reduce",
            "--database", source, "--admin-database", admin,
            "--oracle", oracle, "--oracle-json", "--timeout", "120",
            "--candidate-backend", backend,
            "--output", str(self.output), "--report", str(self.report),
        ]  # fmt: skip
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            command, env=process_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )  # fmt: skip
        self.stdout = self.stderr = ""

    def wait_for_file(self, name: str, what: str) -> Path:
        path = self.state / name

        def reached():
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise AssertionError(f"DBReduce exited before {what}:\n{out}\n{err}")
            return path.exists()

        wait_for(reached, timeout=EXTERNAL_TIMEOUT, what=what)
        return path

    def blocked(self) -> dict:
        self.wait_for_file("blocked.json", "the oracle was running")
        return json.loads((self.state / "blocked.json").read_text())

    def send(self, signum, *, group=False):
        if group:
            os.killpg(self.process.pid, signum)
        else:
            os.kill(self.process.pid, signum)

    def finish(self, timeout=EXTERNAL_TIMEOUT) -> int:
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill_everything()
            self.stdout, self.stderr = self.process.communicate()
            pytest.fail(f"DBReduce did not exit within {timeout} s:\n{self.stderr}")
        self.elapsed = time.monotonic() - self.started
        return self.process.returncode

    def kill_everything(self):
        """Test-side cleanup of the process tree, independent of DBReduce's own."""
        for pid in marked_processes(self.run_id):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def oracle_calls(self) -> int:
        path = self.state / "calls"
        return len(path.read_text().split()) if path.exists() else 0


@pytest.fixture
def start(env):
    runs: list[Run] = []

    def factory(**kwargs) -> Run:
        run = Run(env, **kwargs)
        runs.append(run)
        return run

    yield factory
    for run in runs:
        if run.process.poll() is None:
            run.kill_everything()
            run.process.communicate()
        else:
            run.kill_everything()


def assert_isolated(env, run: Run, *, expect_dirs=True):
    """The source, the foreign database and its live session are untouched; nothing published."""
    assert env.fingerprint() == env.initial_fingerprint
    assert env.lookalike in env.server.names()
    assert env.server.comment(env.lookalike) is None
    assert env.foreign_session.execute("SELECT count(*) FROM keep").fetchone()[0] == 1
    with env.server.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = %s AND datname = %s",
                (env.foreign_pid, env.foreign),
            ).fetchone()[0]
            == 1
        )
    assert not run.output.exists() and not run.report.exists()
    assert not list(run.env.tmp.glob(".dbreduce-*"))
    if expect_dirs:
        assert list(run.cli_tmp.iterdir()) == []


def assert_no_owned_leftovers(env, run: Run):
    assert env.server.generated() - env.before == set()
    assert marked_processes(run.run_id) == []


# -- SIGTERM and SIGINT while an oracle is running --------------------------------------


@pytest.mark.parametrize(
    ("backend", "block_on", "oracle_mode", "target"),
    [
        ("snapshot", 2, "term", "process"),
        ("snapshot", 2, "term", "group"),
        ("snapshot", 2, "ignore", "process"),
        ("clone", 3, "term", "process"),
    ],
)
def test_sigterm_during_active_oracle_cleans_everything_owned(
    env, start, backend, block_on, oracle_mode, target
):
    run = start(backend=backend, block_on=block_on, oracle_mode=oracle_mode)
    info = run.blocked()
    owned_now = env.server.generated() - env.before
    assert len(owned_now) >= (4 if backend == "clone" else 1)
    assert info["database"] in owned_now
    oracle_pid = info["pid"]
    run.send(signal.SIGTERM, group=target == "group")
    code = run.finish()
    assert code == 128 + signal.SIGTERM, run.stderr
    assert "Cancelled by SIGTERM" in run.stderr
    assert "Cleanup complete" in run.stderr
    assert "NOT complete" not in run.stderr
    assert run.elapsed < 30
    assert run.oracle_calls() == block_on  # no oracle call started after the signal
    assert not process_alive(oracle_pid)
    assert (run.state / "term-seen").exists() is (oracle_mode == "term")
    assert_no_owned_leftovers(env, run)
    assert_isolated(env, run)


def test_sigint_keeps_expected_behavior(env, start):
    run = start()
    run.blocked()
    run.send(signal.SIGINT)
    code = run.finish()
    assert code == 128 + signal.SIGINT, run.stderr
    assert "Cancelled by SIGINT" in run.stderr and "Cleanup complete" in run.stderr
    assert_no_owned_leftovers(env, run)
    assert_isolated(env, run)


def test_unfinished_create_of_this_run_is_cancelled_and_other_sessions_are_left_alone(env):
    """The client vanishes without cancelling; only the session named after the database stops."""
    from dbreduce.postgres.database import settle_unproven_create

    name = f"dbreduce_{uuid.uuid4().hex}"
    mine = psycopg.connect(env.server.dsn, autocommit=True, application_name=name)
    other = psycopg.connect(env.server.dsn, autocommit=True, application_name="someone_else")
    other_pid = other.info.backend_pid

    def hold(connection):
        try:
            connection.execute("SELECT pg_sleep(3600)")
        except psycopg.Error:
            pass

    threads = [threading.Thread(target=hold, args=(c,), daemon=True) for c in (mine, other)]
    for thread in threads:
        thread.start()

    def active(application):
        with env.server.connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = %s AND state = 'active'",
                (application,),
            ).fetchone()[0]

    try:
        wait_for(lambda: active(name) and active("someone_else"), what="both sessions running")
        with socket.socket(fileno=os.dup(mine.pgconn.socket)) as connection_socket:
            connection_socket.shutdown(socket.SHUT_RDWR)  # lost connection, no cancel request
        assert settle_unproven_create(env.server.dsn, name) is False
        assert active(name) == 0
        assert active("someone_else") == 1
    finally:
        with env.server.connect() as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (other_pid,))
        for thread in threads:
            thread.join(timeout=30)
        mine.close()
        other.close()


# -- stages that need a controlled hold ---------------------------------------------------


def test_sigterm_while_pg_restore_and_its_child_hold_the_pipes(env, start):
    bin_dir = env.tmp / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pg_restore"
    shim.write_text(PG_RESTORE_SHIM)
    shim.chmod(0o755)
    run = start(path_prefix=str(bin_dir))
    run.wait_for_file("restore.started", "pg_restore was running")
    assert len(env.server.generated() - env.before) == 1  # the workspace, mid-restore
    run.send(signal.SIGTERM)
    code = run.finish()
    assert code == 128 + signal.SIGTERM, run.stderr
    assert "Cleanup complete" in run.stderr
    assert run.elapsed < 30  # not held back by the child that kept the pipes open
    for name in ("restore.pid", "restore-child.pid"):
        assert not process_alive(int((run.state / name).read_text()))
    assert_no_owned_leftovers(env, run)
    assert_isolated(env, run)


@pytest.mark.parametrize("backend", ["snapshot", "clone"])
def test_cancel_after_create_before_ownership_is_recorded_reports_uncertainty(env, start, backend):
    run = start(backend=backend, hook="after_create", hook_nth=1 if backend == "snapshot" else 2)
    created = run.wait_for_file("hook-database", "CREATE DATABASE returned").read_text()
    # The hook sleeps right after the server created the database, before the marker.
    wait_for(lambda: created in env.server.names(), what="the database appearing")
    assert env.server.comment(created) is None
    run.send(signal.SIGTERM)
    code = run.finish()
    try:
        assert code == 1, run.stderr
        assert "Cancelled by SIGTERM" in run.stderr
        assert "Cleanup is NOT complete" in run.stderr
        assert created in run.stderr and "creation outcome unverified" in run.stderr
        # A name alone is not ownership proof: the database was reported, not dropped.
        assert created in env.server.names()
        assert env.server.comment(created) is None
        # Everything that did carry this run's marker was removed.
        unmarked = env.server.generated() - env.before - {created}
        assert unmarked == set()
        assert marked_processes(run.run_id) == []
        assert_isolated(env, run)
    finally:
        env.server.drop(created)


def test_cancel_during_create_stops_the_statement_still_running_on_the_server(env, start):
    run = start(hook="during_create")
    name = run.wait_for_file("hook-database", "CREATE DATABASE was issued").read_text()

    def statement_running():
        with env.server.connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s "
                "AND state = 'active' AND query LIKE 'SELECT pg_sleep%%'",
                (name,),
            ).fetchone()[0]

    wait_for(statement_running, what="the statement running on the server")
    run.send(signal.SIGTERM)
    code = run.finish()
    assert code == 128 + signal.SIGTERM, run.stderr
    assert "Cleanup complete" in run.stderr
    with env.server.connect() as conn:  # the server-side statement did not outlive the run
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (name,)
            ).fetchone()[0]
            == 0
        )
    assert name not in env.server.names()
    assert_no_owned_leftovers(env, run)
    assert_isolated(env, run)


# -- PostgreSQL unavailable or busy during cleanup --------------------------------------


class Proxy:
    """A TCP forwarder the test can cut, to make the server unreachable on demand."""

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
                upstream = socket.create_connection(self.target)
            except OSError:
                return
            self.sockets += [client, upstream]
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pipe(source, sink):
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def stop(self):
        try:
            self.listener.shutdown(socket.SHUT_RDWR)  # wakes accept(); close() alone does not
        except OSError:
            pass
        self.listener.close()
        for s in self.sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            except OSError:
                pass


def test_unreachable_server_during_cleanup_is_finite_and_reported(env, start):
    params = conninfo_to_dict(env.server.dsn)
    proxy = Proxy(str(params["host"]), int(params.get("port", 5432)))
    try:
        via_proxy = make_conninfo(env.server.dsn, host="127.0.0.1", port=proxy.port)
        run = start(dsn=via_proxy)
        info = run.blocked()
        workspace = info["database"]
        proxy.stop()  # the server disappears while the oracle runs
        run.send(signal.SIGTERM)
        code = run.finish()
        assert code == 1, run.stderr
        assert run.elapsed < 40
        assert "Cancelled by SIGTERM" in run.stderr
        assert "Cleanup is NOT complete" in run.stderr
        assert workspace in run.stderr and "result unknown" in run.stderr
        assert "temporary directory removed" in run.stderr  # local cleanup is reported apart
        assert "Cleanup complete" not in run.stderr
        # The report gives the way to check ownership; the database is still there, marked.
        assert "shobj_description" in run.stderr and "dbreduce-workspace:" in run.stderr
        assert env.server.comment(workspace).startswith("dbreduce-workspace:")
        assert marked_processes(run.run_id) == []
        assert_isolated(env, run)
    finally:
        proxy.stop()
        # What the report tells an operator to do: verify the comment, then drop by exact name.
        for name in env.server.generated() - env.before:
            if str(env.server.comment(name) or "").startswith("dbreduce-workspace:"):
                env.server.drop(name)


def advisory_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


def waiting_for_advisory_lock(env) -> bool:
    with env.server.connect() as conn:
        return bool(
            conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ).fetchone()[0]
        )


def test_repeated_signals_do_not_interrupt_cleanup(env, start):
    run = start()
    workspace = run.blocked()["database"]
    holder = env.server.connect()
    holder.execute("SELECT pg_advisory_lock(%s)", (advisory_key(workspace),))
    try:
        run.send(signal.SIGTERM)  # cleanup now waits for the workspace lock
        wait_for(lambda: waiting_for_advisory_lock(env), what="cleanup waiting for the lock")
        run.send(signal.SIGTERM)
        run.send(signal.SIGINT)
        time.sleep(1)
        assert run.process.poll() is None  # extra signals did not abort the cleanup
        holder.execute("SELECT pg_advisory_unlock(%s)", (advisory_key(workspace),))
        code = run.finish()
    finally:
        holder.close()
    assert code == 128 + signal.SIGTERM, run.stderr
    assert "again" in run.stderr and "SIGQUIT" in run.stderr
    assert "Cleanup complete" in run.stderr
    assert_no_owned_leftovers(env, run)
    assert_isolated(env, run)


def test_sigquit_abandons_a_stuck_cleanup(env, start):
    run = start()
    workspace = run.blocked()["database"]
    holder = env.server.connect()
    holder.execute("SELECT pg_advisory_lock(%s)", (advisory_key(workspace),))
    try:
        run.send(signal.SIGTERM)
        wait_for(lambda: waiting_for_advisory_lock(env), what="cleanup waiting for the lock")
        run.send(signal.SIGQUIT)
        code = run.finish(timeout=20)
    finally:
        holder.close()
        run.kill_everything()
    assert code == -signal.SIGQUIT
    # Abandoning cleanup is documented to leave the workspace; the test removes it.
    assert workspace in env.server.names()


def test_cleanup_wait_for_a_lock_is_bounded_by_the_shutdown_budget(env, start):
    run = start(budget=8, margin=6)
    workspace = run.blocked()["database"]
    holder = env.server.connect()
    holder.execute("SELECT pg_advisory_lock(%s)", (advisory_key(workspace),))
    try:
        run.send(signal.SIGTERM)
        code = run.finish(timeout=40)
    finally:
        holder.close()
    assert code == 1, run.stderr
    assert 7 <= run.elapsed < 8 + 6, run.elapsed
    assert "forcing exit" not in run.stderr  # cooperative cleanup gave up on its own
    assert "Cleanup is NOT complete" in run.stderr
    assert workspace in run.stderr and "not removed" in run.stderr
    assert env.server.comment(workspace).startswith("dbreduce-workspace:")
    assert marked_processes(run.run_id) == []
    assert_isolated(env, run)


# -- ordinary reduction is unchanged ------------------------------------------------------


@pytest.mark.parametrize("backend", ["snapshot", "clone"])
def test_successful_reduction_keeps_failure_identity_and_export_reproduces(env, start, backend):
    run = start(backend=backend, block_on=0)
    code = run.finish()
    assert code == 0, run.stderr
    report = json.loads(run.report.read_text())
    assert report["final_rows"] == 2
    assert report["failure_identity"]["preserved"] is True
    expected = "sha256:" + hashlib.sha256(b"BUG_A").hexdigest()
    assert report["failure_identity"]["expected_signature"] == expected
    assert report["database_stats"]["cleanup_failures"] == 0
    assert_no_owned_leftovers(env, run)
    assert env.fingerprint() == env.initial_fingerprint
    # A fresh restore of the exported SQL still shows the same failure.
    restored = report["restore_database"]
    subprocess.run(
        ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", env.server.dsn, "-f", str(run.output)],
        check=True,
        capture_output=True,
    )
    try:
        verdict = subprocess.run(
            [sys.executable, str(env.tmp / "oracle.py")],
            env={
                **os.environ,
                "DATABASE_URL": database_url(env.server.dsn, restored),
                "TEST_STATE": str(run.state),
                "TEST_BLOCK_ON": "0",
                "TEST_ORACLE_MODE": "term",
            },
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(verdict.stdout) == {"reproduced": True, "signature": "BUG_A"}
    finally:
        env.server.drop(restored)
