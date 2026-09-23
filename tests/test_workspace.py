import os
import subprocess
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from dbreduce.postgres.database import Workspace, read_archive_settings
from dbreduce.postgres.dump import client, restore


def test_plain_sql_rejected_before_any_external_command(tmp_path):
    source = tmp_path / "input.sql"
    source.write_text("\\connect original\nDROP TABLE important;\n")
    with patch("dbreduce.postgres.dump.client") as client:
        with pytest.raises(ValueError, match="custom-format"):
            restore("postgresql:///disposable", source)
        client.assert_not_called()


def test_cleanup_targets_only_generated_database():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    with patch("dbreduce.postgres.database.psycopg.connect", return_value=connection):
        with Workspace("postgresql:///original") as workspace:
            workspace.create()
            name = workspace.name
    statements = [
        query.as_string() if hasattr(query, "as_string") else query
        for call in connection.execute.call_args_list
        for query in [call.args[0]]
    ]
    assert name.startswith("dbreduce_")
    assert statements == [
        "SET statement_timeout = '600s'",
        "SELECT pg_advisory_lock(%s)",
        f'CREATE DATABASE "{name}" TEMPLATE template0',
        "SET statement_timeout = '600s'",
        "SELECT pg_advisory_lock(%s)",
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
    ]
    lock_calls = [
        call
        for call in connection.execute.call_args_list
        if call.args[0] == "SELECT pg_advisory_lock(%s)"
    ]
    assert len(lock_calls) == 2
    assert lock_calls[0].args[1] == lock_calls[1].args[1]


def test_restore_failure_still_drops_created_copy(tmp_path):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    with patch("dbreduce.postgres.database.psycopg.connect", return_value=connection):
        with patch(
            "dbreduce.postgres.database.restore", side_effect=RuntimeError("restore failed")
        ):
            with pytest.raises(RuntimeError, match="restore failed"):
                with Workspace("postgresql:///original") as workspace:
                    workspace.reset(tmp_path / "dump")
    assert not workspace.created
    statement = connection.execute.call_args.args[0].as_string()
    assert statement.startswith('DROP DATABASE IF EXISTS "dbreduce_')


def test_interrupt_after_create_still_drops_copy():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.side_effect = [KeyboardInterrupt, None]
    with patch("dbreduce.postgres.database.psycopg.connect", return_value=connection):
        with pytest.raises(KeyboardInterrupt):
            with Workspace("postgresql:///original") as workspace:
                workspace.create()
    assert not workspace.created
    statement = connection.execute.call_args.args[0].as_string()
    assert statement.startswith('DROP DATABASE IF EXISTS "dbreduce_')


def test_uncertain_create_result_still_attempts_cleanup():
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(query, *_args):
        if hasattr(query, "as_string") and query.as_string().startswith("CREATE DATABASE"):
            raise psycopg.OperationalError("server reply lost")

    connection.execute.side_effect = execute
    with patch("dbreduce.postgres.database.psycopg.connect", return_value=connection):
        with pytest.raises(psycopg.OperationalError, match="server reply lost"):
            with Workspace("postgresql:///original") as workspace:
                workspace.create()
    assert not workspace.created
    assert connection.execute.call_args.args[0].as_string() == (
        f'DROP DATABASE IF EXISTS "{workspace.name}" WITH (FORCE)'
    )


def test_existing_database_is_never_dropped_after_name_collision():
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(query, *_args):
        if hasattr(query, "as_string") and query.as_string().startswith("CREATE DATABASE"):
            raise psycopg.errors.DuplicateDatabase("already exists")

    connection.execute.side_effect = execute
    with patch("dbreduce.postgres.database.psycopg.connect", return_value=connection):
        with pytest.raises(psycopg.errors.DuplicateDatabase):
            with Workspace("postgresql:///original") as workspace:
                workspace.create()
    assert not workspace.created
    assert not any(
        "DROP DATABASE" in call.args[0].as_string()
        for call in connection.execute.call_args_list
        if hasattr(call.args[0], "as_string")
    )


@pytest.mark.parametrize(
    ("operation_error", "expected"),
    [
        (ValueError("invalid source"), "invalid source"),
        (psycopg.OperationalError("password=syntheticsecret"), "PostgreSQL operation failed"),
    ],
)
def test_cleanup_failure_keeps_original_reason_without_leaking_database_error(
    operation_error, expected
):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    with patch(
        "dbreduce.postgres.database.psycopg.connect",
        side_effect=[connection, psycopg.OperationalError("server unavailable")],
    ):
        with pytest.raises(RuntimeError) as error:
            with Workspace("postgresql:///original") as workspace:
                workspace.create()
                raise operation_error
    assert expected in str(error.value)
    assert workspace.name in str(error.value)
    assert "syntheticsecret" not in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)


def test_inline_sslpassword_is_rejected_before_spawning():
    with patch("dbreduce.postgres.dump.subprocess.run") as run:
        with pytest.raises(ValueError, match="sslpassword"):
            client(["pg_dump"], "dbname=test password=dbsecret sslpassword=keysecret")
        run.assert_not_called()


def test_database_password_not_in_process_arguments():
    with patch("dbreduce.postgres.dump.subprocess.run") as run:
        run.return_value.returncode = 0
        client(["pg_dump"], "dbname=test password=syntheticsecret")
        args = run.call_args.args[0]
        assert "syntheticsecret" not in str(args)
        assert run.call_args.kwargs["env"]["PGPASSWORD"] == "syntheticsecret"


def test_database_client_timeout_is_reported():
    with patch(
        "dbreduce.postgres.dump.subprocess.run",
        side_effect=subprocess.TimeoutExpired("pg_dump", 600),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            client(["pg_dump"], "dbname=test")


def test_database_client_error_classified_without_leaking_details():
    with patch("dbreduce.postgres.dump.subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stderr = "password authentication failed: syntheticsecret"
        with pytest.raises(RuntimeError, match="authentication failed") as error:
            client(["pg_dump"], "dbname=test password=syntheticsecret")
    assert "syntheticsecret" not in str(error.value)


@pytest.mark.parametrize("encoding", ["LATIN1", "SQL_ASCII"])
def test_archive_settings_ignore_non_utf8_schema_text(tmp_path, encoding):
    archive = tmp_path / "source.dump"
    archive.write_bytes(b"PGDMP")
    stdout = (
        b"-- Non-UTF8 identifier: \xff\n"
        b"CREATE DATABASE original WITH TEMPLATE = template0 "
        + f"ENCODING = '{encoding}' LOCALE_PROVIDER = libc LOCALE = 'C';\n".encode()
    )

    with patch("dbreduce.postgres.database.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = stdout
        settings = read_archive_settings(archive)
        assert run.call_args.args[0][3:5] == ["--use-list", os.devnull]
    assert settings.encoding == encoding
    assert settings.lc_collate == settings.lc_ctype == "C"


def test_archive_with_newline_database_name_reports_pg_restore_rejection(tmp_path):
    archive = tmp_path / "source.dump"
    archive.write_bytes(b"PGDMP")
    with patch("dbreduce.postgres.database.subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stderr = (
            b"pg_restore: error: database name contains a newline or carriage return: "
            b'"source\\nname"'
        )
        with pytest.raises(ValueError, match="name containing a newline or carriage return"):
            read_archive_settings(archive)
        assert run.call_args.kwargs["env"]["LC_ALL"] == "C"
