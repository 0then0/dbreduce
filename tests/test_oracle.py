import shlex
import sys
import time

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


@pytest.mark.parametrize("framed", [False, True])
def test_json_false_is_passed(framed):
    verdict = ("DBREDUCE_VERDICT " if framed else "") + '{"reproduced": false}'
    oracle = Oracle(command(f"print({verdict!r})"), structured=not framed, framed=framed)
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "passed"


def test_framed_json_ignores_application_logs_and_locks_identity():
    oracle = Oracle(
        command(
            "print('startup log'); "
            'print(\'DBREDUCE_VERDICT {"reproduced":true,"signature":"BUG_A"}\'); '
            "print('shutdown log')"
        ),
        framed=True,
    )
    assert oracle.fails("postgresql:///test", lambda: None)
    oracle.command = command(
        "print('startup log'); "
        'print(\'DBREDUCE_VERDICT {"reproduced":true,"signature":"BUG_B"}\')'
    )
    assert not oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "different_failure"
    assert "BUG_A" not in str(oracle.identity_report())
    assert "startup log" not in str(oracle.identity_report())


@pytest.mark.parametrize(
    "lines",
    [
        ["ordinary log"],
        ["DBREDUCE_VERDICT garbage"],
        [
            'DBREDUCE_VERDICT {"reproduced":false,"outcome":"candidate_invalid"}',
            'DBREDUCE_VERDICT {"reproduced":false,"outcome":"candidate_invalid"}',
        ],
        [
            'DBREDUCE_VERDICT {"reproduced":true,"signature":"A"}',
            'DBREDUCE_VERDICT {"reproduced":true,"signature":"A"}',
        ],
    ],
)
def test_framed_json_requires_one_valid_verdict(lines):
    oracle = Oracle(command("\n".join(f"print({line!r})" for line in lines)), framed=True)
    with pytest.raises(OracleError):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"


def test_strict_json_still_rejects_noisy_stdout():
    oracle = Oracle(
        command('print(\'log\'); print(\'{"reproduced":true,"signature":"A"}\')'),
        structured=True,
    )
    with pytest.raises(OracleError, match="Invalid structured oracle JSON"):
        oracle.fails("postgresql:///test", lambda: None)


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


def test_supervisor_kills_background_oracle_child(tmp_path):
    marker = tmp_path / "background-finished"
    child = command(f"import pathlib,time; time.sleep(0.3); pathlib.Path({str(marker)!r}).touch()")
    assert Oracle(f"{child} & exit 1").fails("postgresql:///test", lambda: None)
    time.sleep(0.4)
    assert not marker.exists()


def test_regex_matcher_timeout_is_separate_from_oracle_exit():
    oracle = Oracle(
        command("import sys; sys.stdout.write('a' * 28 + '!'); sys.exit(1)"),
        match_stdout="(a+)+$",
        timeout=0.2,
    )
    with pytest.raises(OracleError, match="timed out"):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "timeout"
    assert oracle.outcomes["timeout"] == 1


@pytest.mark.parametrize("framed", [False, True])
def test_candidate_invalid_rejects_without_confirming_or_changing_identity(framed):
    import json

    oracle = Oracle("", structured=not framed, framed=framed, confirm=3)
    oracle.expected_signature = "sha256:target"
    verdict = json.dumps({"reproduced": False, "outcome": "candidate_invalid"})
    lines = ["log", "DBREDUCE_VERDICT " + verdict, "log"] if framed else [verdict]
    oracle.command = command("\n".join(f"print({line!r})" for line in lines))
    calls = []
    assert not oracle.fails("postgresql:///test", lambda: calls.append(1), lambda: calls.append(2))
    assert oracle.last_outcome == "candidate_invalid"
    assert oracle.outcomes["candidate_invalid"] == 1
    assert oracle.executions == 1
    assert calls == [1, 2]
    assert oracle.expected_signature == "sha256:target"
    assert oracle.final_signature is None


@pytest.mark.parametrize("framed", [False, True])
@pytest.mark.parametrize(
    "fields",
    [
        {"reproduced": True, "outcome": "candidate_invalid", "signature": "BUG_A"},
        {"reproduced": False, "outcome": "unknown"},
        {"reproduced": False, "outcome": None},
        {"reproduced": False, "outcome": []},
        {"reproduced": False, "outcome": 1},
        {"reproduced": False, "outcome": "candidate_invalid", "signature": "BUG_B"},
        {"reproduced": False, "outcome": "candidate_invalid", "signature": None},
        {"reproduced": False, "outcome": "candidate_invalid", "error": "offline"},
    ],
)
def test_invalid_outcome_fields_fail_closed(framed, fields):
    import json

    verdict = ("DBREDUCE_VERDICT " if framed else "") + json.dumps(fields)
    oracle = Oracle(command(f"print({verdict!r})"), structured=not framed, framed=framed)
    with pytest.raises(OracleError):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"


@pytest.mark.parametrize("framed", [False, True])
def test_candidate_invalid_requires_zero_exit(framed):
    verdict = ("DBREDUCE_VERDICT " if framed else "") + (
        '{"reproduced":false,"outcome":"candidate_invalid"}'
    )
    oracle = Oracle(
        command(f"print({verdict!r}); raise SystemExit(1)"), structured=not framed, framed=framed
    )
    with pytest.raises(OracleError):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"


@pytest.mark.parametrize("first", ["same_failure", "candidate_invalid"])
def test_confirmation_rejects_mixed_validity(first):
    import json

    oracle = Oracle("", structured=True, confirm=3)
    oracle.expected_signature = "sha256:target"
    verdicts = iter(
        [
            {"reproduced": True, "signature": "BUG_A"}
            if first == "same_failure"
            else {"reproduced": False, "outcome": "candidate_invalid"},
            {"reproduced": False, "outcome": "candidate_invalid"},
        ]
    )
    if first == "same_failure":
        import hashlib

        oracle.expected_signature = "sha256:" + hashlib.sha256(b"BUG_A").hexdigest()

    def prepare():
        verdict = json.dumps(next(verdicts))
        oracle.command = command(f"print({verdict!r})")

    assert not oracle.fails("postgresql:///test", prepare)
    assert oracle.executions == (2 if first == "same_failure" else 1)
    assert oracle.last_outcome == "candidate_invalid"


def test_unknown_application_crash_remains_infrastructure_error():
    oracle = Oracle(
        command(
            "import sys; print('PG::UndefinedTable Redis unavailable', "
            "file=sys.stderr); raise SystemExit(1)"
        ),
        structured=True,
    )
    with pytest.raises(OracleError):
        oracle.fails("postgresql:///test", lambda: None)
    assert oracle.last_outcome == "infrastructure_error"
