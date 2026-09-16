import copy
from typing import Any

from aegisops.domain import EvidenceView, RootCause
from aegisops.security.guards import canonical


def build_context(
    incident: dict[str, Any], evidence: list[EvidenceView], cap: int, correction: str = ""
) -> dict[str, Any]:
    # Scenario IDs, ground-truth labels and replay failure scripts never enter the model context.
    data: dict[str, Any] = {
        "incident": {k: incident[k] for k in ("title", "service", "severity")},
        "evidence": [e.model_dump() for e in sorted(evidence, key=lambda e: e.observed_at)],
        "validation_feedback": correction,
    }
    result = copy.deepcopy(data)
    for e in result["evidence"]:
        if len(canonical(result).encode()) <= cap:
            break
        e["payload_excerpt"] = []
        e["truncated"] = True
    while result["evidence"] and len(canonical(result).encode()) > cap:
        result["evidence"].pop(0)
    return result


def insufficient(service: str) -> RootCause:
    return RootCause.model_validate(
        dict(
            cause="insufficient_evidence",
            affected_service=service,
            confidence=0.1,
            evidence_ids=[],
            reasoning_summary="Evidence was unavailable or could not be validated.",
            falsification_test="Collect additional independent telemetry before making a diagnosis.",
        )
    )
