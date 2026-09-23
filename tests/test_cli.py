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
