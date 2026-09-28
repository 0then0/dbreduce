import json

import pytest
from typer.testing import CliRunner

from dbreduce.cli import app, publish_result
from dbreduce.postgres.database import database_url


def test_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reduce" in result.output and "inspect" in result.output


def test_requires_exactly_one_source():
    result = CliRunner().invoke(app, ["reduce", "--oracle", "false"])
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_dump_requires_admin():
    result = CliRunner().invoke(app, ["reduce", "--dump", "fixture.sql", "--oracle", "false"])
    assert result.exit_code == 1
    assert "--admin-database" in result.output


def test_connection_url_replaces_source_database():
    from psycopg.conninfo import conninfo_to_dict

    url = database_url("postgresql://a:p%40ss@localhost/original?sslmode=require", "dbreduce_123")
    assert url.startswith("postgresql://a:p%40ss@localhost/dbreduce_123")
    params = conninfo_to_dict(url)
    assert params["dbname"] == "dbreduce_123"
    assert params["password"] == "p@ss"
    assert params["sslmode"] == "require"


def test_publish_rolls_back_if_report_parent_missing(tmp_path):
    exported = tmp_path / "result.sql"
    exported.write_text("SELECT 1;")
    output = tmp_path / "min.sql"
    with pytest.raises(FileNotFoundError):
        publish_result(exported, output, tmp_path / "missing" / "report.json", {})
    assert not output.exists()
    assert list(tmp_path.glob(".dbreduce-*")) == []


def test_publish_does_not_replace_existing_report(tmp_path):
    exported = tmp_path / "result.sql"
    exported.write_text("SELECT 1;")
    output = tmp_path / "min.sql"
    report = tmp_path / "report.json"
    report.write_text("owned by someone else")
    with pytest.raises(FileExistsError):
        publish_result(exported, output, report, {})
    assert not output.exists()
    assert report.read_text() == "owned by someone else"


def test_publish_complete_and_private(tmp_path):
    exported = tmp_path / "result.sql"
    exported.write_text("SELECT 1;")
    output = tmp_path / "min.sql"
    report = tmp_path / "report.json"
    publish_result(exported, output, report, {"rows_by_table": {"a.b": {"c": 1}}})
    assert output.read_text() == "SELECT 1;"
    assert json.loads(report.read_text())["rows_by_table"] == {"a.b": {"c": 1}}
    assert output.stat().st_mode & 0o077 == 0
    assert report.stat().st_mode & 0o077 == 0


def test_publish_rolls_back_partial_sql_write(tmp_path, monkeypatch):
    from dbreduce import cli

    exported = tmp_path / "result.sql"
    exported.write_text("SELECT 1;")
    output = tmp_path / "min.sql"
    report = tmp_path / "report.json"

    def fail_after_partial_write(source, destination):
        destination.write(b"SELECT")
        raise OSError("disk full")

    monkeypatch.setattr(cli.shutil, "copyfileobj", fail_after_partial_write)
    with pytest.raises(OSError, match="disk full"):
        publish_result(exported, output, report, {})
    assert not output.exists() and not report.exists()
    assert list(tmp_path.glob(".dbreduce-*")) == []


def test_reduce_json_identity_report(tmp_path, monkeypatch):
    import shlex
    import sys
    from unittest.mock import MagicMock

    from dbreduce import cli
    from dbreduce.models.schema import Schema, Table

    connection = MagicMock()
    connection.__enter__.return_value = connection
    workspace = MagicMock()
    workspace.__enter__.return_value = workspace
    workspace.name = "dbreduce_test"
    workspace.url = "postgresql:///dbreduce_test"
    workspace.databases_created = 0
    workspace.databases_dropped = 0
    workspace.cleanup_failures = 0
    backend = MagicMock(probes=3, accepted=1, constraint_rejections=0, raise_rejections=0)
    monkeypatch.setattr(cli, "require_clients", lambda: None)
    monkeypatch.setattr(cli.psycopg, "connect", lambda *a, **kw: connection)
    monkeypatch.setattr(cli, "Workspace", lambda *a, **kw: workspace)
    monkeypatch.setattr(cli, "read_settings", lambda _: None)
    monkeypatch.setattr(cli, "check_extension_tables", lambda _: None)
    monkeypatch.setattr(
        cli, "inspect_database", lambda _: Schema((Table(("public", "items"), ("id",), 2),), ())
    )
    monkeypatch.setattr(cli, "PostgresBackend", lambda *a: backend)
    monkeypatch.setattr(cli, "reduce_state", lambda *a: {("public", "items"): [("x", 0)]})
    monkeypatch.setattr(cli, "dump", lambda dsn, path, **kw: path.write_text("SELECT 1;"))
    code = 'print(\'{"reproduced": true, "signature": "BUG_A"}\')'
    result = CliRunner().invoke(
        app,
        [
            "reduce",
            "--database",
            "postgresql:///source",
            "--oracle-json",
            "--oracle",
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
            "--confirm",
            "2",
            "--output",
            str(tmp_path / "result.sql"),
            "--report",
            str(tmp_path / "report.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "report.json").read_text())
    identity = report["failure_identity"]
    assert identity["mode"] == "json" and identity["preserved"]
    assert identity["expected_signature"] == identity["final_signature"]
    assert report["oracle_executions"] == 4
    assert report["oracle_stats"]["outcomes"] == {"same_failure": 4}
    assert report["candidate_stats"] == {"created": 3, "accepted": 1, "rejected": 2}
    assert "BUG_A" not in (tmp_path / "report.json").read_text()


def test_inspect_virtual_relationship_label(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from dbreduce import cli
    from dbreduce.models.schema import Schema, Table

    connection = MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(cli.psycopg, "connect", lambda *a, **kw: connection)
    monkeypatch.setattr(
        cli,
        "inspect_database",
        lambda _: Schema(
            (
                Table(("public", "child"), (), 2),
                Table(("public", "parent"), (), 2),
            ),
            (),
        ),
    )
    config = tmp_path / "relations.json"
    config.write_text(
        json.dumps(
            {
                "relationships": [
                    {
                        "from": {"table": "child", "columns": ["parent_id"]},
                        "to": {"table": "parent", "columns": ["id"]},
                    }
                ]
            }
        )
    )
    result = CliRunner().invoke(
        app, ["inspect", "--database", "postgresql:///source", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert "virtual parent_id -> public.parent(id)" in result.output
    assert "public.child -> public.parent" in result.output


def test_conflicting_identity_flags_rejected_before_connection():
    result = CliRunner().invoke(
        app,
        [
            "reduce",
            "--database",
            "postgresql:///source",
            "--oracle",
            "false",
            "--oracle-json",
            "--match-stderr",
            "BUG",
        ],
    )
    assert result.exit_code == 1
    assert "cannot be combined" in result.output
