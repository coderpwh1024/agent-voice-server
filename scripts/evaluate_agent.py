"""Run versioned Agent contract evaluations without calling live model providers."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[1]
Outcome = Literal["passed", "failed", "skipped", "error"]


class ContractCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    title: str = Field(min_length=1)
    target: str = Field(
        pattern=r"^tests/(?:[a-zA-Z0-9_]+/)*test_[a-zA-Z0-9_]+\.py::test_[a-zA-Z0-9_]+$"
    )


class ContractSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    cases: list[ContractCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> "ContractSuite":
        for values in ([case.id for case in self.cases], [case.target for case in self.cases]):
            if len(values) != len(set(values)):
                raise ValueError("Case IDs and test targets must be unique")
        return self


class TestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    outcome: Outcome
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)


class CaseResult(BaseModel):
    id: str
    category: str
    title: str
    target: str
    passed: bool
    tests: list[TestResult]


class ContractReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["contract"] = "contract"
    suite_name: str
    suite_sha256: str
    created_at: str
    metadata: dict[str, str]
    pytest_exit_code: int | None
    issues: list[str]
    passed: bool
    cases: list[CaseResult]
    regressions: list[str] = Field(default_factory=list)


def select_cases(suite: ContractSuite, categories: list[str]) -> list[ContractCase]:
    unknown = set(categories) - {case.category for case in suite.cases}
    if unknown:
        raise ValueError(f"Unknown categories: {', '.join(sorted(unknown))}")
    return [case for case in suite.cases if not categories or case.category in categories]


def parse_junit(path: Path) -> list[TestResult]:
    results: dict[str, TestResult] = {}
    severity = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
    for element in ET.parse(path).iter("testcase"):
        file = element.get("file")
        name = element.get("name")
        if not file or not name:
            raise ValueError("JUnit contains a collection error or an unidentified test")
        node_id = f"{file}::{name}"
        outcome: Outcome = "passed"
        for candidate in ("skipped", "failed", "error"):
            tag = "failure" if candidate == "failed" else candidate
            if element.find(tag) is not None:
                outcome = candidate
        result = TestResult(
            node_id=node_id,
            outcome=outcome,
            duration_seconds=float(element.get("time", "0")),
        )
        previous = results.get(node_id)
        if previous is not None:
            result.duration_seconds += previous.duration_seconds
            if severity[previous.outcome] > severity[outcome]:
                result.outcome = previous.outcome
        results[node_id] = result
    return list(results.values())


def matches_target(node_id: str, target: str) -> bool:
    return node_id == target or node_id.startswith(target + "[")


def build_report(
    suite: ContractSuite,
    cases: list[ContractCase],
    tests: list[TestResult],
    exit_code: int | None,
    issues: list[str],
    metadata: dict[str, str],
) -> ContractReport:
    issues = list(issues)
    results = []
    for case in cases:
        selected = [test for test in tests if matches_target(test.node_id, case.target)]
        results.append(
            CaseResult(
                **case.model_dump(),
                passed=bool(selected) and all(test.outcome == "passed" for test in selected),
                tests=selected,
            )
        )
    if any(not any(matches_target(test.node_id, case.target) for case in cases) for test in tests):
        issues.append("unexpected_tests")
    return ContractReport(
        suite_name=suite.name,
        suite_sha256=hashlib.sha256(
            json.dumps(suite.model_dump(), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        created_at=datetime.now(UTC).isoformat(),
        metadata=metadata,
        pytest_exit_code=exit_code,
        issues=issues,
        passed=bool(results)
        and exit_code == 0
        and not issues
        and all(result.passed for result in results),
        cases=results,
    )


def compare_baseline(current: ContractReport, baseline: ContractReport) -> list[str]:
    def signature(report: ContractReport) -> dict[str, list[str]]:
        return {case.id: sorted(test.node_id for test in case.tests) for case in report.cases}

    if (
        current.suite_sha256 != baseline.suite_sha256
        or signature(current) != signature(baseline)
        or not baseline.cases
        or any(not case.tests for case in baseline.cases)
        or baseline.pytest_exit_code not in (0, 1)
        or baseline.issues
    ):
        raise ValueError("Baseline is incomplete or has a different suite, selection or test set")
    previous = {test.node_id: test.outcome for case in baseline.cases for test in case.tests}
    return sorted(
        test.node_id
        for case in current.cases
        for test in case.tests
        if previous[test.node_id] == "passed" and test.outcome != "passed"
    )


def source_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def run_metadata() -> dict[str, str]:
    metadata = {
        "python": platform.python_version(),
        "platform": platform.system() + "-" + platform.machine(),
        "app_sha256": source_digest(list((ROOT / "app").rglob("*.py"))),
        "tests_sha256": source_digest(
            list((ROOT / "tests").rglob("*.py")) + list((ROOT / "tests/contracts").glob("*.json"))
        ),
        "runner_sha256": source_digest(
            [Path(__file__).resolve(), *list((ROOT / "evaluations").glob("*.py"))]
        ),
        "lock_sha256": source_digest([ROOT / "uv.lock", ROOT / "pyproject.toml"]),
        "prompts": "synthetic pytest fixtures; production Nacos prompts not evaluated",
        "models": "mock/fake; no live quality measurement",
    }
    for package in ("pytest", "langgraph", "langchain-core", "pydantic", "langfuse"):
        try:
            metadata[package] = version(package)
        except PackageNotFoundError:
            metadata[package] = "not-installed"
    for label, args in (
        ("git_commit", ["rev-parse", "HEAD"]),
        ("git_dirty", ["status", "--porcelain", "--untracked-files=normal"]),
    ):
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False, timeout=10
        )
        metadata[label] = (
            str(bool(result.stdout.strip())).lower()
            if label == "git_dirty"
            else result.stdout.strip()
        )
    return metadata


def run_contracts(suite: ContractSuite, cases: list[ContractCase], timeout: int) -> ContractReport:
    metadata = run_metadata()
    with TemporaryDirectory(prefix="agent-contract-") as directory:
        junit = Path(directory) / "results.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "evaluations.offline",
            *[case.target for case in cases],
            "--import-mode=importlib",
            "-q",
            "--tb=no",
            "-o",
            "addopts=",
            "-o",
            "junit_family=legacy",
            "-o",
            "junit_logging=no",
            "-o",
            "xfail_strict=true",
            f"--junitxml={junit}",
        ]
        try:
            process = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTEST_ADDOPTS": ""},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return build_report(suite, cases, [], None, ["pytest_timeout"], metadata)
        try:
            tests = parse_junit(junit)
        except (OSError, ET.ParseError, ValueError):
            return build_report(
                suite, cases, [], process.returncode, ["invalid_or_missing_junit"], metadata
            )
    finished = run_metadata()
    fingerprint_keys = ("app_sha256", "tests_sha256", "runner_sha256", "lock_sha256")
    issues = (
        ["workspace_changed_during_run"]
        if any(metadata.get(key) != finished.get(key) for key in fingerprint_keys)
        else []
    )
    return build_report(suite, cases, tests, process.returncode, issues, metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "evaluations/agent_contracts.json")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "output/evaluations/contracts.json")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        suite = ContractSuite.model_validate_json(args.manifest.read_text())
        cases = select_cases(suite, args.category)
        if args.timeout <= 0:
            raise ValueError("Timeout must be positive")
        for case in cases:
            if not (ROOT / case.target.split("::")[0]).is_file():
                raise ValueError(f"Test file missing for {case.id}")
        if args.list:
            for case in cases:
                print(f"{case.category}: {case.id} — {case.title}")
            return 0
        if args.baseline and args.baseline.resolve() == args.output.resolve():
            raise ValueError("Output must not overwrite the baseline")
        baseline = (
            ContractReport.model_validate_json(args.baseline.read_text()) if args.baseline else None
        )
        print(f"Running {len(cases)} contract scenarios (mock/fake models).", flush=True)
        report = run_contracts(suite, cases, args.timeout)
        comparison_error = False
        if baseline is not None:
            try:
                report.regressions = compare_baseline(report, baseline)
            except ValueError:
                report.issues.append("incomparable_baseline")
                report.passed = False
                comparison_error = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, ValidationError) as exc:
        print(f"Evaluation configuration error ({type(exc).__name__}); check paths and schema.")
        return 2
    for category in sorted({case.category for case in report.cases}):
        results = [case for case in report.cases if case.category == category]
        print(f"{category}: {sum(case.passed for case in results)}/{len(results)} scenarios passed")
    for case in report.cases:
        if not case.tests:
            print(f"MISSING: {case.id} ({case.target})")
        for test in case.tests:
            if test.outcome != "passed":
                print(f"{test.outcome.upper()}: {test.node_id}")
    if report.issues:
        print(f"Issues: {', '.join(report.issues)}")
    if baseline is not None:
        print(f"Regressions: {len(report.regressions)}")
    print(f"Gate: {'PASS' if report.passed else 'FAIL'}. Report: {args.output}")
    return 2 if comparison_error else (0 if report.passed else 1)


if __name__ == "__main__":
    raise SystemExit(main())
