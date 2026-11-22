"""Command-line interface for local runs and CI."""

import argparse
import sys
from pathlib import Path

from .agents import Agent, capture, make_runner
from .dataset import Case, InputError, bounded_int, load_cases
from .gate import Policy, compare
from .report import write_json, write_report

EXAMPLES = Path(__file__).resolve().parent / "examples"
DEFAULT_SUITE = EXAMPLES / "refund_cases.jsonl"
DEFAULT_POLICY = EXAMPLES / "gate_policy.json"


def suite_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", type=Path, default=DEFAULT_SUITE, help="Labeled refund cases in JSON Lines.")
    parser.add_argument("--case", action="append", default=[], metavar="ID",
                        help="Run a single case for diagnosis. Repeat to select more cases.")
    parser.add_argument("--timeout", type=int, default=45, metavar="SECONDS", help="Ollama request timeout.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ai-release-gate", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Show a blocked release and a corrected release offline.")
    demo.add_argument("--out-dir", type=Path, default=Path("reports/demo"))
    comparison = commands.add_parser("compare", help="Evaluate two versions and return the release verdict.")
    suite_args(comparison)
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--candidate", type=Path, required=True)
    comparison.add_argument("--baseline-recording", type=Path)
    comparison.add_argument("--candidate-recording", type=Path)
    comparison.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    comparison.add_argument("--offline", action="store_true", help="Require recordings for model agents.")
    comparison.add_argument("--out-dir", type=Path, default=Path("reports/comparison"))
    recording = commands.add_parser("record", help="Capture a live Ollama agent for reproducible offline replay.")
    suite_args(recording)
    recording.add_argument("--agent", type=Path, required=True)
    recording.add_argument("--output", type=Path, required=True)
    return root


def select(cases: list[Case], requested: list[str]) -> list[Case]:
    if not requested:
        return cases
    unknown = set(requested).difference(case.id for case in cases)
    if unknown:
        raise InputError(f"Unknown case IDs: {', '.join(sorted(unknown))}.")
    return [case for case in cases if case.id in requested]


def build_report(cases: list[Case], total: int, baseline: Agent, candidate: Agent, policy: Policy,
                 base_recording: Path | None = None, candidate_recording: Path | None = None,
                 offline: bool = True, timeout: int = 45) -> dict:
    base_mode, run_base = make_runner(baseline, cases, base_recording, offline, timeout)
    candidate_mode, run_candidate = make_runner(candidate, cases, candidate_recording, offline, timeout)
    return compare(cases, baseline, candidate, base_mode, candidate_mode, run_base, run_candidate, policy, total)


def show(directory: Path, report: dict) -> None:
    write_report(directory, report)
    candidate = report["candidate"]
    label = f"{report['verdict']} (partial suite)" if report["suite"]["selected"] != report["suite"]["total"] else report["verdict"]
    print(f"{label} | {candidate['id']} | {candidate['passed']}/{candidate['total']} passed | "
          f"{candidate['unsafe_approvals']} unsafe approvals")
    for failure in report["failures"]:
        print(f"  {failure['rule']}: {failure['message']}")
    print(f"  {directory / 'report.md'}")


def run_demo(directory: Path) -> int:
    cases = load_cases(DEFAULT_SUITE)
    policy = Policy.load(DEFAULT_POLICY)
    agents = EXAMPLES / "agents"
    baseline = Agent.load(agents / "demo-baseline.json")
    bad = build_report(cases, len(cases), baseline, Agent.load(agents / "relaxed.json"), policy)
    fixed = build_report(cases, len(cases), baseline, Agent.load(agents / "corrected.json"), policy)
    show(directory / "blocked", bad)
    show(directory / "corrected", fixed)
    return 0 if bad["candidate"]["unsafe_approvals"] > 0 and bad["verdict"] == "BLOCKED" and fixed["verdict"] == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            return run_demo(args.out_dir)
        timeout = bounded_int(args.timeout, 1, 600)
        cases = load_cases(args.dataset)
        selected = select(cases, args.case)
        if args.command == "record":
            agent = Agent.load(args.agent)
            write_json(args.output, capture(agent, selected, timeout))
            print(f"Recorded {len(selected)} decisions for {agent.id} to {args.output}.")
            return 0
        report = build_report(selected, len(cases), Agent.load(args.baseline), Agent.load(args.candidate),
                              Policy.load(args.policy), args.baseline_recording, args.candidate_recording,
                              args.offline, timeout)
        show(args.out_dir, report)
        return 0 if report["verdict"] == "PASS" else 1
    except (InputError, OSError) as exc:
        print(f"Release gate error: {exc}", file=sys.stderr)
        return 2
