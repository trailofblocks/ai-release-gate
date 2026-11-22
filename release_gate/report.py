"""Write portable JSON and a review-friendly Markdown scorecard."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        target.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, document: dict) -> None:
    write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def outcome(evaluation: dict) -> str:
    if evaluation["result"] is None:
        return evaluation["error"]
    return decision(evaluation["result"]["decision"])


def decision(result: dict) -> str:
    return f"`{result['action']} / {result['reason']}`"


def render(document: dict) -> str:
    suite = document["suite"]
    lines = [
        f"# AI Release Gate · {document['verdict']}",
        "",
        f"Evaluated {suite['selected']} of {suite['total']} cases.",
    ]
    if suite["selected"] != suite["total"]:
        lines += ["", "**Partial run.** Use a full run for a release decision."]
    lines += [
        "",
        "| Agent | Mode | Passed | Unsafe approvals | Errors | p95 latency | Input / output tokens |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for side in ("baseline", "candidate"):
        summary = document[side]
        p95 = "—" if summary["p95_ms"] is None else f"{summary['p95_ms']} ms"
        input_tokens, output_tokens = summary["input_tokens"], summary["output_tokens"]
        tokens = "—" if input_tokens is None or output_tokens is None else f"{input_tokens} / {output_tokens}"
        lines.append(
            f"| `{summary['id']}` | {summary['mode']} | {summary['passed']}/{summary['total']} "
            f"({summary['pass_percent']}%) | {summary['unsafe_approvals']} | {summary['execution_errors']} | {p95} | {tokens} |"
        )
    if document["failures"]:
        lines += ["", "## Blocking checks", ""]
        lines.extend(f"- **{failure['rule']}** — {failure['message']}" for failure in document["failures"])
    rows = [row for row in document["cases"] if not row["baseline"]["passed"] or not row["candidate"]["passed"]]
    if rows:
        lines += ["", "## Cases to inspect"]
    for row in rows:
        lines += [
            "", f"### {row['id']}", "",
            f"- Expected {decision(row['expected'])}. {row['rule']}",
            f"- Baseline {outcome(row['baseline'])}",
            f"- Candidate {outcome(row['candidate'])}",
        ]
        if row["lookup"] is not None:
            lookup = row["lookup"]
            args = lookup["arguments"]
            result = json.dumps(lookup["result"], sort_keys=True)
            lines.append(f"- Order lookup `lookup_order({args['order_id']}) → {result}`")
    if not rows:
        lines += ["", "All evaluated decisions matched the labeled policy."]
    return "\n".join(lines) + "\n"


def write_report(directory: Path, document: dict) -> None:
    write_json(directory / "report.json", document)
    write_text(directory / "report.md", render(document))
