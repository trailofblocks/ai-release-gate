import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from release_gate.agents import Agent, AgentError, Result, make_runner
from release_gate.cli import DEFAULT_POLICY, DEFAULT_SUITE, EXAMPLES, build_report, main
from release_gate.dataset import InputError, load_cases
from release_gate.gate import Policy, compare
from release_gate.report import write_json


class ReleaseGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(DEFAULT_SUITE)
        cls.agents = EXAMPLES / "agents"
        cls.current = Agent.load(cls.agents / "demo-baseline.json")

    def test_demo_and_reports(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["demo", "--out-dir", directory]), 0)
            blocked_dir = Path(directory, "blocked")
            blocked = json.loads((blocked_dir / "report.json").read_text(encoding="utf-8"))
            corrected = json.loads(Path(directory, "corrected", "report.json").read_text(encoding="utf-8"))
            self.assertEqual(blocked["candidate"]["passed"], 42)
            self.assertEqual(blocked["new_failures"], [f"window-day-{day}" for day in (31, 32, 35, 39, 44, 45)])
            self.assertEqual(corrected["candidate"]["passed"], 48)
            self.assertEqual(corrected["verdict"], "PASS")
            self.assertIn("lookup_order(R-0111)", (blocked_dir / "report.md").read_text(encoding="utf-8"))

    def test_cli_gate_exit_status_and_partial_report(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            code = main(["compare", "--offline", "--baseline", str(self.agents / "demo-baseline.json"),
                         "--candidate", str(self.agents / "relaxed.json"), "--case", "window-day-31",
                         "--out-dir", directory])
            self.assertEqual(code, 1)
            self.assertIn("Partial run", Path(directory, "report.md").read_text(encoding="utf-8"))

    def test_unsafe_approval_blocks_even_when_both_versions_agree(self):
        bad = Agent.load(self.agents / "relaxed.json")
        report = build_report(self.cases, len(self.cases), bad, bad, Policy(0, 100, None))
        self.assertEqual(report["new_failures"], [])
        self.assertEqual([failure["rule"] for failure in report["failures"]], ["UNSAFE_APPROVAL"])

    def test_errors_and_unavailable_latency_fail_closed(self):
        case = self.cases[0]

        def outage(_case):
            raise AgentError("Ollama is unreachable or timed out.")

        report = compare([case], self.current, self.current, "live", "live", outage,
                         lambda _: Result(case.expected), Policy(0, 100, 100), 1)
        self.assertEqual([item["rule"] for item in report["failures"]], ["EXECUTION_ERROR", "LATENCY_BUDGET"])
        self.assertIsNone(report["candidate"]["p95_ms"])

    def test_recordings_bind_to_agent_and_case_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "sample.json")
            model = Agent.load(self.agents / "ollama-current.json")
            one_case = self.cases[:1]
            case = one_case[0]
            _, run = make_runner(self.current, one_case, None, True, 1)
            one_report = build_report(one_case, 1, self.current, self.current, Policy.load(DEFAULT_POLICY))
            document = {"version": 1, "agent_sha256": model.digest(), "cases": {
                case.id: {"input_sha256": one_report["cases"][0]["input_sha256"],
                          "result": one_report["cases"][0]["candidate"]["result"]},
            }}
            write_json(path, document)
            mode, replay = make_runner(model, one_case, path, True, 1)
            self.assertEqual(mode, "recording")
            self.assertEqual(replay(case), run(case))
            with self.assertRaisesRegex(InputError, "does not match"):
                make_runner(Agent.load(self.agents / "ollama-relaxed.json"), one_case, path, True, 1)
            with self.assertRaisesRegex(InputError, "missing case"):
                make_runner(model, self.cases[:2], path, True, 1)
            document["cases"][case.id]["input_sha256"] = "stale"
            write_json(path, document)
            with self.assertRaisesRegex(InputError, "stale input"):
                make_runner(model, one_case, path, True, 1)
            with self.assertRaisesRegex(InputError, "requires a recording"):
                make_runner(model, one_case, None, True, 1)

    def test_invalid_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "duplicate.jsonl")
            first_line = DEFAULT_SUITE.read_text(encoding="utf-8").splitlines()[0]
            path.write_text(first_line + "\n" + first_line, encoding="utf-8")
            with self.assertRaisesRegex(InputError, "line 2: Duplicate"):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
