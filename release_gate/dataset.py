"""Load a labeled suite and build input from a read-only order lookup."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASONS = {
    "MISSING_ORDER_ID": ("request_info", "Ask for an order reference."),
    "ORDER_NOT_FOUND": ("escalate", "Escalate when the order service finds no record."),
    "OWNERSHIP_UNVERIFIED": ("escalate", "Escalate if ownership cannot be verified."),
    "ALREADY_REFUNDED": ("deny", "Deny a second refund."),
    "PAYMENT_PENDING": ("escalate", "Escalate an unsettled payment."),
    "ORDER_NOT_DELIVERED": ("escalate", "Escalate if delivery has not completed."),
    "FINAL_SALE": ("deny", "Deny a final-sale refund."),
    "WINDOW_EXPIRED": ("deny", "Deny if more than 30 days have elapsed since delivery."),
    "ELIGIBLE": ("approve", "Approve when none of the earlier rules apply."),
}


class InputError(ValueError):
    """An invalid suite, agent specification, policy, or recording."""


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InputError(f"Could not read JSON at {path}: {error}") from error


def fields(raw: Any, keys: set[str]) -> dict:
    if not isinstance(raw, dict) or raw.keys() != keys:
        raise InputError(f"Expected fields {', '.join(sorted(keys))}.")
    return raw


def identifier(raw: Any) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", raw):
        raise InputError("Identifiers must be 1–80 letters, digits, hyphens, periods, or underscores.")
    return raw


def bounded_int(raw: Any, low: int, high: int) -> int:
    if type(raw) is not int or not low <= raw <= high:
        raise InputError(f"Expected an integer from {low} to {high}.")
    return raw


def fingerprint(raw: Any) -> str:
    payload = json.dumps(raw, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str

    @classmethod
    def parse(cls, raw: Any) -> "Decision":
        fields(raw, {"action", "reason"})
        action, reason = raw["action"], raw["reason"]
        if not isinstance(reason, str) or reason not in REASONS or REASONS[reason][0] != action:
            raise InputError("The action and reason must form a valid decision pair.")
        return cls(action, reason)


@dataclass(frozen=True)
class Case:
    id: str
    ticket: str
    order_id: str | None
    order: dict | None
    expected: Decision

    def input(self) -> dict:
        lookup = None
        if self.order_id is not None:
            lookup = {"tool": "lookup_order", "arguments": {"order_id": self.order_id},
                      "result": self.order}
        return {"ticket": self.ticket, "order_id": self.order_id, "lookup": lookup}

    @classmethod
    def parse(cls, raw: Any) -> "Case":
        fields(raw, {"id", "ticket", "order_id", "order", "expected"})
        ticket, order_id, order = raw["ticket"], raw["order_id"], raw["order"]
        if not isinstance(ticket, str) or not ticket.strip() or len(ticket) > 4000:
            raise InputError("Ticket must be a non-empty string with at most 4000 characters.")
        if order_id is not None:
            identifier(order_id)
        if order is not None:
            if order_id is None:
                raise InputError("An order record requires an order reference.")
            fields(order, {"ownership_verified", "refunded", "payment_settled", "days_since_delivery", "final_sale"})
            for key in ("ownership_verified", "refunded", "payment_settled", "final_sale"):
                if type(order[key]) is not bool:
                    raise InputError(f"{key} must be a boolean.")
            if order["days_since_delivery"] is not None:
                bounded_int(order["days_since_delivery"], 0, 100_000)
        return cls(identifier(raw["id"]), ticket, order_id, order, Decision.parse(raw["expected"]))


def load_cases(path: Path) -> list[Case]:
    cases = []
    seen = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as error:
        raise InputError(f"Could not read suite at {path}: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            case = Case.parse(json.loads(line))
            if case.id in seen:
                raise InputError(f"Duplicate case ID {case.id}.")
        except ValueError as error:
            raise InputError(f"{path}, line {line_number}: {error}") from error
        cases.append(case)
        seen.add(case.id)
    if not cases:
        raise InputError(f"Suite is empty at {path}.")
    return cases
