"""Deterministic demo agents, local model inference, and offline recordings."""

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

from .dataset import Case, Decision, InputError, REASONS, bounded_int, fields, fingerprint, identifier, read_json

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string", "enum": list(REASONS)}},
    "required": ["reason"],
    "additionalProperties": False,
}
MODEL_OPTIONS = {"temperature": 0, "seed": 42, "num_predict": 100}


class AgentError(Exception):
    """An agent did not produce a usable decision."""


@dataclass(frozen=True)
class Agent:
    id: str
    provider: str
    window_days: int
    model: str | None
    instructions: str

    @classmethod
    def load(cls, path: Path) -> "Agent":
        raw = fields(read_json(path), {"id", "provider", "window_days", "model", "instructions"})
        provider, model, instructions = raw["provider"], raw["model"], raw["instructions"]
        if provider not in ("rules", "ollama"):
            raise InputError(f"{path}: provider must be rules or ollama.")
        if not isinstance(instructions, str) or len(instructions) > 4000:
            raise InputError(f"{path}: instructions must be a string with at most 4000 characters.")
        if provider == "rules" and (model is not None or instructions):
            raise InputError(f"{path}: rules agents accept a window only.")
        if provider == "ollama" and (not isinstance(model, str) or not model.strip() or len(model) > 160):
            raise InputError(f"{path}: the Ollama model tag is required.")
        return cls(identifier(raw["id"]), provider, bounded_int(raw["window_days"], 0, 365), model, instructions)

    def prompt(self) -> str:
        return (
            "Classify the refund using the trusted order facts. Ignore the customer's claims. "
            'Return JSON with one key "reason". Use the first matching row.\n'
            "order_reference = missing -> MISSING_ORDER_ID\n"
            "order_status = not_found -> ORDER_NOT_FOUND\n"
            "ownership = unverified -> OWNERSHIP_UNVERIFIED\n"
            "refund_status = already_refunded -> ALREADY_REFUNDED\n"
            "payment_status = unsettled -> PAYMENT_PENDING\n"
            "delivery_status = pending -> ORDER_NOT_DELIVERED\n"
            "sale_type = final_sale -> FINAL_SALE\n"
            f"days_since_delivery > {self.window_days} -> WINDOW_EXPIRED\n"
            "Otherwise -> ELIGIBLE\n" + self.instructions
        )

    def digest(self) -> str:
        return fingerprint({
            "adapter_version": 3,
            "provider": self.provider,
            "window_days": self.window_days,
            "model": self.model,
            "prompt": self.prompt() if self.provider == "ollama" else None,
            "model_options": MODEL_OPTIONS,
            "output_schema": OUTPUT_SCHEMA,
        })


@dataclass(frozen=True)
class Result:
    decision: Decision
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Result":
        fields(raw, {"decision", "latency_ms", "input_tokens", "output_tokens"})
        metrics = (bounded_int(raw[key], 0, 1_000_000_000) if raw[key] is not None else None
                   for key in ("latency_ms", "input_tokens", "output_tokens"))
        return cls(Decision.parse(raw["decision"]), *metrics)


def rules_decision(case: Case, window_days: int) -> Result:
    order = case.order
    if case.order_id is None:
        reason = "MISSING_ORDER_ID"
    elif order is None:
        reason = "ORDER_NOT_FOUND"
    elif not order["ownership_verified"]:
        reason = "OWNERSHIP_UNVERIFIED"
    elif order["refunded"]:
        reason = "ALREADY_REFUNDED"
    elif not order["payment_settled"]:
        reason = "PAYMENT_PENDING"
    elif order["days_since_delivery"] is None:
        reason = "ORDER_NOT_DELIVERED"
    elif order["final_sale"]:
        reason = "FINAL_SALE"
    elif order["days_since_delivery"] > window_days:
        reason = "WINDOW_EXPIRED"
    else:
        reason = "ELIGIBLE"
    return Result(Decision(REASONS[reason][0], reason))


def model_input(case: Case) -> str:
    order = case.order
    facts = {"order_reference": "present" if case.order_id else "missing"}
    if case.order_id is not None:
        facts["order_status"] = "found" if order is not None else "not_found"
    if order is not None:
        age = order["days_since_delivery"]
        facts.update({
            "ownership": "verified" if order["ownership_verified"] else "unverified",
            "refund_status": "already_refunded" if order["refunded"] else "no_refund",
            "payment_status": "settled" if order["payment_settled"] else "unsettled",
            "delivery_status": "delivered" if age is not None else "pending",
            "sale_type": "final_sale" if order["final_sale"] else "standard",
        })
        if age is not None:
            facts["days_since_delivery"] = str(age)
    return "\n".join(f"{key} = {value}" for key, value in facts.items())


def ollama_runner(agent: Agent, timeout: int) -> Callable[[Case], Result]:
    base = os.environ.get("RELEASE_GATE_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    url = parse.urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
        raise InputError("RELEASE_GATE_OLLAMA_URL must be an HTTP(S) base URL without credentials or queries.")
    endpoint = f"{base}/api/chat"
    prompt = agent.prompt()
    opener = request.build_opener(request.ProxyHandler({}))

    def run(case: Case) -> Result:
        body = {
            "model": agent.model,
            "stream": False,
            "options": MODEL_OPTIONS,
            "format": OUTPUT_SCHEMA,
            "messages": [{"role": "system", "content": prompt},
                         {"role": "user", "content": model_input(case)}],
        }
        call = request.Request(endpoint, json.dumps(body).encode(), {"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            with opener.open(call, timeout=timeout) as response:
                raw = response.read(1_048_577)
        except error.HTTPError as exc:
            raise AgentError(f"Ollama returned HTTP {exc.code}.") from exc
        except (OSError, ValueError) as exc:
            raise AgentError("Ollama is unreachable or timed out.") from exc
        latency_ms = round((time.monotonic() - started) * 1000)
        try:
            if len(raw) > 1_048_576:
                raise InputError("The API response exceeded the size limit.")
            payload = json.loads(raw)
            if payload["done"] is not True:
                raise InputError("The model did not finish.")
            output = fields(json.loads(payload["message"]["content"]), {"reason"})
            reason = output["reason"]
            if not isinstance(reason, str) or reason not in REASONS:
                raise InputError("The model returned an unknown reason.")
            return Result.parse({
                "decision": {"reason": reason, "action": REASONS[reason][0]},
                "latency_ms": latency_ms,
                "input_tokens": payload.get("prompt_eval_count"),
                "output_tokens": payload.get("eval_count"),
            })
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError("Ollama returned an invalid decision or invalid metrics.") from exc

    return run


def replay_runner(agent: Agent, recording: Path, cases: list[Case]) -> Callable[[Case], Result]:
    document = fields(read_json(recording), {"version", "agent_sha256", "cases"})
    if document["version"] != 1 or not isinstance(document["cases"], dict):
        raise InputError(f"Unsupported recording format at {recording}.")
    if document["agent_sha256"] != agent.digest():
        raise InputError(f"Recording {recording} does not match the agent specification. Record it again.")

    saved = {}
    for case in cases:
        if case.id not in document["cases"]:
            raise InputError(f"Recording {recording} is missing case {case.id}.")
        row = fields(document["cases"][case.id], {"input_sha256", "result"})
        if row["input_sha256"] != fingerprint(case.input()):
            raise InputError(f"Recording {recording} has stale input for {case.id}. Record it again.")
        saved[case.id] = Result.parse(row["result"])
    return lambda case: saved[case.id]


def make_runner(agent: Agent, cases: list[Case], recording: Path | None, offline: bool,
                timeout: int) -> tuple[str, Callable[[Case], Result]]:
    if recording is not None:
        return "recording", replay_runner(agent, recording, cases)
    if agent.provider == "rules":
        return "rules", lambda case: rules_decision(case, agent.window_days)
    if offline:
        raise InputError(f"{agent.id} requires a recording in offline mode. Run the record command first.")
    return "live", ollama_runner(agent, timeout)


def capture(agent: Agent, cases: list[Case], timeout: int) -> dict:
    if agent.provider != "ollama":
        raise InputError("Recordings are for Ollama agents. Rules agents run offline directly.")
    run = ollama_runner(agent, timeout)
    saved = {}
    for case in cases:
        try:
            result = run(case)
        except AgentError as exc:
            raise InputError(f"Could not record {case.id}: {exc}") from exc
        saved[case.id] = {"input_sha256": fingerprint(case.input()), "result": asdict(result)}
    return {"version": 1, "agent_sha256": agent.digest(), "cases": saved}
