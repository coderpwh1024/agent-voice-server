import ast
import json
import subprocess

import pytest
from pydantic import ValidationError

from scripts import evaluate_agent as evaluation


def suite(*targets: str) -> evaluation.ContractSuite:
    return evaluation.ContractSuite(
        schema_version=1,
        name="test-contracts",
        cases=[
            evaluation.ContractCase(
                id=f"case-{index}", category="safety", title="Synthetic case", target=target
            )
            for index, target in enumerate(targets)
        ],
    )


def result(target: str, outcome: evaluation.Outcome = "passed") -> evaluation.TestResult:
    return evaluation.TestResult(node_id=target, outcome=outcome, duration_seconds=0.01)


def report(
    manifest: evaluation.ContractSuite, tests: list[evaluation.TestResult], exit_code: int = 0
) -> evaluation.ContractReport:
    return evaluation.build_report(manifest, manifest.cases, tests, exit_code, [], {})


def test_manifest_targets_exist_and_are_unique():
    manifest = evaluation.ContractSuite.model_validate_json(
        (evaluation.ROOT / "evaluations/agent_contracts.json").read_text()
    )
    for case in manifest.cases:
        file, name = case.target.split("::")
        tree = ast.parse((evaluation.ROOT / file).read_text())
        assert name in {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    assert len({case.category for case in manifest.cases}) == 9


@pytest.mark.parametrize("duplicate", ["id", "target"])
def test_manifest_rejects_duplicate_scenarios(duplicate):
    manifest = suite("tests/test_example.py::test_one", "tests/test_example.py::test_two")
    raw = manifest.model_dump()
    raw["cases"][1][duplicate] = raw["cases"][0][duplicate]
    with pytest.raises(ValidationError, match="unique"):
        evaluation.ContractSuite.model_validate(raw)


def test_categories_are_explicit_and_unknown_categories_fail():
    manifest = suite("tests/test_example.py::test_one")
    assert evaluation.select_cases(manifest, ["safety"]) == manifest.cases
    with pytest.raises(ValueError, match="Unknown categories"):
        evaluation.select_cases(manifest, ["typo"])


@pytest.mark.parametrize("outcome", ["failed", "skipped", "error"])
def test_any_nonpass_fails_the_gate_including_parameterized_variants(outcome):
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    actual = report(manifest, [result(target + "[a]"), result(target + "[b]", outcome)])
    assert not actual.passed
    assert not actual.cases[0].passed
    assert len(actual.cases[0].tests) == 2


def test_missing_unexpected_and_crashed_tests_cannot_report_success():
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    assert not report(manifest, []).passed
    assert not report(manifest, [result(target)], exit_code=2).passed
    unexpected = report(manifest, [result(target), result(target + "_other")])
    assert unexpected.issues == ["unexpected_tests"]
    assert not unexpected.passed


def test_baseline_compares_individual_trials_and_rejects_different_coverage():
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    baseline = report(manifest, [result(target + "[a]"), result(target + "[b]")])
    current = report(manifest, [result(target + "[a]"), result(target + "[b]", "failed")], 1)
    assert evaluation.compare_baseline(current, baseline) == [target + "[b]"]
    with pytest.raises(ValueError, match="different suite"):
        evaluation.compare_baseline(report(manifest, [result(target + "[a]")]), baseline)
    current.suite_sha256 = "different-version"
    with pytest.raises(ValueError, match="different suite"):
        evaluation.compare_baseline(current, baseline)


def test_baseline_rejects_an_incomplete_run():
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    complete = report(manifest, [result(target)])
    baseline = report(manifest, [result(target)], exit_code=2)
    with pytest.raises(ValueError, match="incomplete"):
        evaluation.compare_baseline(complete, baseline)


def test_junit_omits_captured_content_and_preserves_teardown_errors(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite><testcase file="tests/test_example.py" name="test_one" '
        'time="0.2"><system-out>synthetic private content</system-out></testcase>'
        '<testcase file="tests/test_example.py" name="test_one" time="0.1">'
        "<error>synthetic traceback content</error></testcase></testsuite></testsuites>"
    )
    tests = evaluation.parse_junit(junit)
    assert len(tests) == 1
    assert tests[0].outcome == "error"
    assert tests[0].duration_seconds == pytest.approx(0.3)
    assert "content" not in tests[0].model_dump_json()


def test_actual_pytest_handles_skip_xfail_xpass_setup_and_teardown(tmp_path, monkeypatch):
    root = evaluation.ROOT
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    monkeypatch.setattr(evaluation, "run_metadata", lambda: {})
    monkeypatch.setenv("PYTHONPATH", str(root))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nonexistent")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text(
        "import socket\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken_setup():\n"
        "    raise ValueError('synthetic setup failure')\n"
        "@pytest.fixture\n"
        "def broken_teardown():\n"
        "    yield\n"
        "    raise ValueError('synthetic teardown failure')\n"
        "def test_network():\n"
        "    with pytest.raises(RuntimeError, match='Network access is disabled'):\n"
        "        socket.create_connection(('example.invalid', 443))\n"
        "@pytest.mark.parametrize('number', [1, 2])\n"
        "def test_parameters(number):\n"
        "    assert number > 0\n"
        "def test_skipped():\n"
        "    pytest.skip('not a pass')\n"
        "@pytest.mark.xfail\n"
        "def test_xfailed():\n"
        "    assert False\n"
        "@pytest.mark.xfail\n"
        "def test_xpassed():\n"
        "    pass\n"
        "def test_setup(broken_setup):\n"
        "    pass\n"
        "def test_teardown(broken_teardown):\n"
        "    pass\n"
    )
    names = ["network", "parameters", "skipped", "xfailed", "xpassed", "setup", "teardown"]
    manifest = suite(*(f"tests/test_example.py::test_{name}" for name in names))
    actual = evaluation.run_contracts(manifest, manifest.cases, 30)
    outcomes = {case.target.split("::")[1]: case for case in actual.cases}
    assert outcomes["test_network"].passed
    assert outcomes["test_parameters"].passed
    assert len(outcomes["test_parameters"].tests) == 2
    for name, outcome in [
        ("skipped", "skipped"),
        ("xfailed", "skipped"),
        ("xpassed", "failed"),
        ("setup", "error"),
        ("teardown", "error"),
    ]:
        assert outcomes[f"test_{name}"].tests[0].outcome == outcome
    assert actual.pytest_exit_code == 1
    assert not actual.passed
    assert not actual.issues
    assert "synthetic setup failure" not in actual.model_dump_json()


def test_timeout_and_collection_failure_produce_failed_reports(tmp_path, monkeypatch):
    manifest = suite("tests/test_example.py::test_one")
    monkeypatch.setattr(evaluation, "run_metadata", lambda: {})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("pytest", 1)

    monkeypatch.setattr(evaluation.subprocess, "run", timeout)
    actual = evaluation.run_contracts(manifest, manifest.cases, 1)
    assert actual.issues == ["pytest_timeout"]
    assert not actual.passed
    monkeypatch.setattr(
        evaluation.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 2)
    )
    actual = evaluation.run_contracts(manifest, manifest.cases, 1)
    assert actual.issues == ["invalid_or_missing_junit"]
    assert actual.pytest_exit_code == 2
    assert not actual.passed


def test_source_changes_during_a_run_invalidate_the_report(monkeypatch):
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    fingerprints = iter([{"app_sha256": "before"}, {"app_sha256": "after"}])
    monkeypatch.setattr(evaluation, "run_metadata", lambda: next(fingerprints))
    monkeypatch.setattr(evaluation, "parse_junit", lambda _: [result(target)])
    monkeypatch.setattr(
        evaluation.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
    )
    actual = evaluation.run_contracts(manifest, manifest.cases, 1)
    assert actual.cases[0].passed
    assert actual.issues == ["workspace_changed_during_run"]
    assert not actual.passed


def test_cli_reports_incomparable_baseline_without_overwriting_it(tmp_path, monkeypatch):
    target = "tests/test_example.py::test_one"
    manifest = suite(target)
    manifest_file = tmp_path / "suite.json"
    manifest_file.write_text(manifest.model_dump_json())
    current = report(manifest, [result(target)])
    baseline = current.model_copy(update={"suite_sha256": "other-suite"}, deep=True)
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(baseline.model_dump_json())
    output = tmp_path / "report.json"
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").touch()
    monkeypatch.setattr(evaluation, "run_contracts", lambda *args: current)
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_agent.py",
            "--manifest",
            str(manifest_file),
            "--baseline",
            str(baseline_file),
            "--output",
            str(output),
        ],
    )
    assert evaluation.main() == 2
    raw = json.loads(output.read_text())
    assert raw["issues"] == ["incomparable_baseline"]
    assert not raw["passed"]
    assert baseline_file.read_text() == baseline.model_dump_json()
