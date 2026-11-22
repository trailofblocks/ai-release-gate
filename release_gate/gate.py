"""Score candidate behavior against labels and compare it with a baseline."""

from dataclasses import asdict, dataclass
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import Callable

from .agents import Agent, AgentError, Result
from .dataset import Case, REASONS, bounded_int, fields, fingerprint, read_json


@dataclass(frozen=True)
class Policy:
    min_pass_percent: int
    max_regression_points: int
    max_candidate_p95_ms: int | None

    @classmethod
    def load(cls, path: Path) -> "Policy":
        raw = fields(read_json(path), {"min_pass_percent", "max_regression_points", "max_candidate_p95_ms"})
        p95 = raw["max_candidate_p95_ms"]
        return cls(bounded_int(raw["min_pass_percent"], 0, 100),
                   bounded_int(raw["max_regression_points"], 0, 100),
                   None if p95 is None else bounded_int(p95, 1, 1_000_000))


def evaluate(case: Case, run: Callable[[Case], Result]) -> dict:
    try:
        output = run(case)
    except AgentError as exc:
        return {"passed": False, "unsafe_approval": False, "error": str(exc), "result": None}
    return {
        "passed": output.decision == case.expected,
        "unsafe_approval": output.decision.action == "approve" and case.expected.action != "approve",
        "error": None,
        "result": asdict(output),
    }


def summarize(rows: list[dict], side: str) -> dict:
    evaluations = [row[side] for row in rows]
    results = [evaluation["result"] for evaluation in evaluations if evaluation["result"] is not None]
    passed = sum(evaluation["passed"] for evaluation in evaluations)
    latency = sorted(result["latency_ms"] for result in results if result["latency_ms"] is not None)
    summary = {
        "passed": passed,
        "total": len(rows),
        "pass_percent": round(100 * passed / len(rows), 2),
        "unsafe_approvals": sum(evaluation["unsafe_approval"] for evaluation in evaluations),
        "execution_errors": sum(evaluation["error"] is not None for evaluation in evaluations),
        "p95_ms": latency[ceil(len(latency) * 0.95) - 1] if len(latency) == len(rows) else None,
    }
    for key in ("input_tokens", "output_tokens"):
        tokens = [result[key] for result in results if result[key] is not None]
        summary[key] = sum(tokens) if len(tokens) == len(rows) else None
    return summary


def compare(cases: list[Case], baseline: Agent, candidate: Agent, baseline_mode: str, candidate_mode: str,
            run_baseline: Callable[[Case], Result], run_candidate: Callable[[Case], Result],
            policy: Policy, suite_size: int) -> dict:
    rows = []
    for case in cases:
        expected = asdict(case.expected)
        rows.append({
            "id": case.id, "expected": expected, "rule": REASONS[case.expected.reason][1],
            "lookup": case.input()["lookup"], "input_sha256": fingerprint(case.input()),
            "baseline": evaluate(case, run_baseline), "candidate": evaluate(case, run_candidate),
        })
    before = summarize(rows, "baseline")
    after = summarize(rows, "candidate")
    failures = []
    critical = [row["id"] for row in rows if row["candidate"]["unsafe_approval"]]
    if critical:
        failures.append({"rule": "UNSAFE_APPROVAL", "message": "Candidate approved a request that policy disallows.",
                         "cases": critical})
    for side in ("baseline", "candidate"):
        errors = [row["id"] for row in rows if row[side]["error"] is not None]
        if errors:
            failures.append({"rule": "EXECUTION_ERROR", "message": f"{side.capitalize()} could not complete all cases.",
                             "cases": errors})
    if Fraction(after["passed"] * 100, len(rows)) < policy.min_pass_percent:
        failures.append({"rule": "PASS_RATE", "message": f"Candidate pass rate is below {policy.min_pass_percent}%.",
                         "cases": [row["id"] for row in rows if not row["candidate"]["passed"]]})
    regression = Fraction((before["passed"] - after["passed"]) * 100, len(rows))
    if regression > policy.max_regression_points:
        failures.append({"rule": "REGRESSION", "message": f"Pass rate dropped by more than {policy.max_regression_points} percentage points.",
                         "cases": [row["id"] for row in rows if row["baseline"]["passed"] and not row["candidate"]["passed"]]})
    p95_limit = policy.max_candidate_p95_ms
    if p95_limit is not None and (after["p95_ms"] is None or after["p95_ms"] > p95_limit):
        failures.append({"rule": "LATENCY_BUDGET", "message": f"Candidate did not demonstrate p95 at or below {p95_limit} ms.",
                         "cases": []})
    return {
        "version": 1, "verdict": "BLOCKED" if failures else "PASS",
        "suite": {"selected": len(rows), "total": suite_size,
                  "sha256": fingerprint([{"id": row["id"], "input": row["input_sha256"], "expected": row["expected"]}
                                         for row in rows])},
        "policy": asdict(policy),
        "baseline": {"id": baseline.id, "mode": baseline_mode, "sha256": baseline.digest(), **before},
        "candidate": {"id": candidate.id, "mode": candidate_mode, "sha256": candidate.digest(), **after},
        "new_failures": [row["id"] for row in rows if row["baseline"]["passed"] and not row["candidate"]["passed"]],
        "fixes": [row["id"] for row in rows if not row["baseline"]["passed"] and row["candidate"]["passed"]],
        "failures": failures, "cases": rows,
    }
