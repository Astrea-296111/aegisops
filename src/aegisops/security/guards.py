import hashlib
import json
import re
from typing import Any

from aegisops.domain import ActionProposal, EvidenceView, InvalidEvidence, PolicyDecision, RootCause

CANARY = "AEGIS_DEMO_SECRET_CANARY_7c93"
SECRET_KEYS = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|authorization|access[_-]?token)")
PATTERNS = [
    re.compile(r"(?i)Bearer\s+[\w.\-+/=]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(?:password|api[_-]?key|secret|token)\s*[=:]\s*[\"']?[^\s,;\"'}]+"),
]
INJECTION = re.compile(
    r"(?i)(ignore.{0,40}(instructions|rules)|reveal.{0,30}secret|system\s*prompt|call\s+remediation|<\s*/?\s*(system|assistant)|忽略.{0,15}(指令|规则)|泄露.{0,10}密钥)"
)


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if SECRET_KEYS.search(str(k)) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        result = value.replace(CANARY, "[REDACTED]")
        for pattern in PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result
    return value


def injection_flag(value: Any) -> bool:
    return bool(INJECTION.search(canonical(value)))


def grounded(root: RootCause, evidence: list[EvidenceView]) -> None:
    if root.cause == "insufficient_evidence":
        if root.confidence > 0.3:
            raise InvalidEvidence("insufficient_evidence_confidence")
        return
    index = {e.id: e for e in evidence}
    if not root.evidence_ids or any(i not in index for i in root.evidence_ids):
        raise InvalidEvidence("missing_evidence_id")
    selected = [index[i] for i in set(root.evidence_ids)]
    useful = [e for e in selected if e.payload_excerpt]
    if len(useful) != len(selected):
        raise InvalidEvidence("empty_evidence")
    if root.confidence >= 0.8 and len({e.source_type for e in useful}) < 2:
        raise InvalidEvidence("high_confidence_requires_independent_signal_types")
    # Referential grounding does not prove causality. Check resource relevance too.
    if not any(root.affected_service in canonical(e.payload_excerpt) for e in useful):
        raise InvalidEvidence("evidence_resource_mismatch")


def action_policy(
    action: ActionProposal,
    *,
    state: str,
    role: str,
    approval: dict[str, Any] | None,
    run_id: str,
    now: float,
    current_revision: int,
    cancelled: bool = False,
) -> PolicyDecision:
    reasons = []
    if cancelled:
        reasons.append("cancellation_requested")
    if action.expected_revision != current_revision:
        reasons.append("resource_revision_changed")
    if state == "PROPOSE_REMEDIATION":
        return PolicyDecision(allowed=not reasons, reasons=reasons)
    if state != "EXECUTE_REMEDIATION":
        reasons.append("invalid_state")
    if role != "operator":
        reasons.append("operator_required")
    if approval is None:
        reasons.append("approval_required")
    else:
        if approval.get("run_id") != run_id:
            reasons.append("run_mismatch")
        if approval.get("decision") != "approved":
            reasons.append("not_approved")
        if approval.get("expires_at", 0) <= now:
            reasons.append("approval_expired")
        if approval.get("action_hash") != digest(action.model_dump()):
            reasons.append("action_substitution")
        if approval.get("decided_by") != "operator":
            reasons.append("invalid_approver")
    return PolicyDecision(allowed=not reasons, reasons=reasons)


def event_hash(
    run_id: str, sequence: int, actor: str, kind: str, payload: Any, at: float, previous: str
) -> str:
    return digest(
        {
            "run_id": run_id,
            "sequence": sequence,
            "actor": actor,
            "event_type": kind,
            "payload": payload,
            "created_at": at,
            "previous_hash": previous,
        }
    )


def verify_chain(events: list[dict[str, Any]], expected_head: str | None = None) -> bool:
    previous = ""
    for sequence, e in enumerate(events, 1):
        expected = event_hash(
            e["run_id"],
            sequence,
            e["actor"],
            e["event_type"],
            e["payload"],
            e["created_at"],
            previous,
        )
        if (
            e["sequence"] != sequence
            or e["previous_hash"] != previous
            or e["event_hash"] != expected
        ):
            return False
        previous = expected
    return expected_head is None or previous == expected_head
