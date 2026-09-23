import shlex
import sys

import pytest

from dbreduce.oracle.runner import Oracle, OracleError


def command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_confirm_and_environment():
    calls = []
    oracle = Oracle(
        command(
            "import os,sys; sys.exit(5 if os.environ['DATABASE_URL'] == "
            "os.environ['DBREDUCE_DATABASE_URL'] == 'postgresql:///test' else 0)"
        ),
        confirm=3,
    )
    assert oracle.fails("postgresql:///test", lambda: calls.append(1))
    assert oracle.executions == len(calls) == 3


def test_stops_on_first_success():
    oracle = Oracle(command("pass"), confirm=3)
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.executions == 1


@pytest.mark.parametrize("command_text", ["exit 126", "exit 127", "kill -TERM $$"])
def test_every_nonzero_status_reproduces(command_text):
    assert Oracle(command_text).fails("postgresql:///test", lambda: None)


def test_missing_shell_command_has_nonzero_status():
    assert Oracle("dbreduce-nonexistent-command").fails("postgresql:///test", lambda: None)


def test_timeout_is_not_failure():
    with pytest.raises(OracleError, match="timed out"):
        Oracle(command("import time; time.sleep(10)"), timeout=0.1).fails(
            "postgresql:///test", lambda: None
        )
