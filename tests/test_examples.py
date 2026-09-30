import os
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "examples/netbox-17498/wrapper.py"


@pytest.mark.parametrize(
    "url,host,port,user,password",
    [
        (
            "postgresql://postgres@candidate-host/candidate_db",
            "candidate-host",
            5432,
            "postgres",
            "",
        ),
        (
            "postgresql:///candidate_db",
            "/tmp/candidate-socket",
            6543,
            "resolved-user",
            "resolved-pass",
        ),
    ],
)
def test_netbox_wrapper_forwards_effective_connection_parameters(
    monkeypatch, url, host, port, user, password
):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = ("required_table",)
    connection.info = SimpleNamespace(
        dbname="candidate_db", host=host, port=port, user=user, password=password
    )
    for setting in ("DB_NAME", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
        monkeypatch.setenv(setting, "inherited-source-value")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kwargs: connection)
    forwarded = {}

    def exec_oracle(executable, arguments):
        forwarded.update(
            {
                key: os.environ[key]
                for key in ("DB_NAME", "DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD")
            }
        )

    monkeypatch.setattr(os, "execv", exec_oracle)
    runpy.run_path(str(WRAPPER), run_name="__main__")
    assert forwarded == {
        "DB_NAME": "candidate_db",
        "DB_HOST": host,
        "DB_PORT": str(port),
        "DB_USER": user,
        "DB_PASSWORD": password,
    }


def test_netbox_wrapper_connection_failure_never_executes_application(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql:///candidate_db")

    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("connection unavailable")

    execute = MagicMock()
    monkeypatch.setattr(psycopg, "connect", unavailable)
    monkeypatch.setattr(os, "execv", execute)
    with pytest.raises(psycopg.OperationalError):
        runpy.run_path(str(WRAPPER), run_name="__main__")
    execute.assert_not_called()
    assert capsys.readouterr().out == ""


def test_netbox_wrapper_missing_table_emits_only_invalid_verdict(monkeypatch, capsys):
    import json

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (None,)
    execute = MagicMock()
    monkeypatch.setenv("DATABASE_URL", "postgresql:///candidate_db")
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kwargs: connection)
    monkeypatch.setattr(os, "execv", execute)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(WRAPPER), run_name="__main__")
    assert stopped.value.code == 0
    execute.assert_not_called()
    assert json.loads(capsys.readouterr().out) == {
        "reproduced": False,
        "outcome": "candidate_invalid",
    }
