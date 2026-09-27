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
def test_infrastructure_status_aborts(command_text):
    with pytest.raises(OracleError, match="infrastructure"):
        Oracle(command_text).fails("postgresql:///test", lambda: None)


def test_missing_shell_command_has_nonzero_status():
    with pytest.raises(OracleError, match="infrastructure"):
        Oracle("dbreduce-nonexistent-command").fails("postgresql:///test", lambda: None)


def test_timeout_is_not_failure():
    with pytest.raises(OracleError, match="timed out"):
        Oracle(command("import time; time.sleep(10)"), timeout=0.1).fails(
            "postgresql:///test", lambda: None
        )


def test_regex_identity_and_different_failure():
    oracle = Oracle(
        command("import sys; print('BUG_A', file=sys.stderr); sys.exit(1)"),
        match_stderr="BUG_A",
        confirm=2,
    )
    assert oracle.fails("postgresql:///test", lambda: None)
    oracle.command = command("import sys; print('BUG_B', file=sys.stderr); sys.exit(1)")
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "different_failure"
    assert oracle.executions == 3


def test_json_locks_initial_signature():
    oracle = Oracle(
        command('print(\'{"reproduced": true, "signature": "BUG_A"}\')'), structured=True
    )
    assert oracle.fails("postgresql:///test", lambda: None)
    oracle.command = command('print(\'{"reproduced": true, "signature": "BUG_B"}\')')
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "different_failure"
    assert "BUG_A" not in str(oracle.identity_report())


@pytest.mark.parametrize(
    "verdict",
    [
        "garbage",
        "[]",
        '{"reproduced": 1}',
        '{"reproduced": true}',
        '{"reproduced": false, "error": "db offline"}',
    ],
)
def test_invalid_json_verdict_is_infrastructure_error(verdict):
    oracle = Oracle(command(f"print({verdict!r})"), structured=True)
    with pytest.raises(OracleError):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"


def test_confirmation_rejects_mixed_identity():
    oracle = Oracle("", structured=True, confirm=3)
    signatures = iter(["BUG_A", "BUG_B", "BUG_A"])

    def prepare():
        import json

        verdict = json.dumps({"reproduced": True, "signature": next(signatures)})
        oracle.command = command(f"print({verdict!r})")

    assert not oracle.fails("postgresql:///test", prepare)
    assert oracle.executions == 2


def test_exit_match_and_stdout_match_are_conjunctive():
    oracle = Oracle(
        command("print('BUG'); raise SystemExit(2)"), expected_exit_code=1, match_stdout="BUG"
    )
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "different_failure"


def test_matcher_locks_initial_exit_code():
    oracle = Oracle(command("print('BUG'); raise SystemExit(1)"), match_stdout="BUG")
    assert oracle.fails("postgresql:///test", lambda: None)
    oracle.command = command("print('BUG'); raise SystemExit(2)")
    assert not oracle.fails("postgresql:///test", lambda: None)


def test_json_false_is_passed():
    oracle = Oracle(command("print('{\"reproduced\": false}')"), structured=True)
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "passed"


def test_output_limit_aborts_during_execution():
    oracle = Oracle(
        command("import sys,time; print('x' * (2 * 1024 * 1024), flush=True); time.sleep(10)"),
        match_stdout="x",
        timeout=2,
    )
    with pytest.raises(OracleError, match="1 MiB"):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"


def test_legacy_discards_large_output():
    oracle = Oracle(command("print('x' * (2 * 1024 * 1024)); raise SystemExit(1)"))
    assert oracle.fails("postgresql:///test", lambda: None)


def test_unused_stderr_is_discarded():
    oracle = Oracle(
        command(
            "import sys; print('x' * (2 * 1024 * 1024), file=sys.stderr); "
            "print('BUG'); raise SystemExit(1)"
        ),
        match_stdout="BUG",
    )
    assert oracle.fails("postgresql:///test", lambda: None)


def test_background_child_cannot_hold_pipe_open():
    oracle = Oracle("sleep 10 & printf BUG; exit 1", match_stdout="BUG", timeout=1)
    assert oracle.fails("postgresql:///test", lambda: None)


def test_timeout_with_captured_output():
    oracle = Oracle(
        command("import time; print('BUG', flush=True); time.sleep(10)"),
        match_stdout="BUG",
        timeout=0.1,
    )
    with pytest.raises(OracleError, match="timed out"):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.outcomes["timeout"] == 1
