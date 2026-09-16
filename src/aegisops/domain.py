from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Service = Literal["order-service", "inventory-service"]
ToolName = Literal[
    "query_metrics", "query_logs", "query_traces", "service_topology", "get_deploy_metadata"
]
Cause = Literal[
    "downstream_timeout", "downstream_500", "overload", "bad_release", "insufficient_evidence"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class State(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGE = "TRIAGE"
    PLAN = "PLAN"
    COLLECT = "COLLECT"
    HYPOTHESIZE = "HYPOTHESIZE"
    VERIFY = "VERIFY"
    PROPOSE_REMEDIATION = "PROPOSE_REMEDIATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTE_REMEDIATION = "EXECUTE_REMEDIATION"
    VERIFY_REMEDIATION = "VERIFY_REMEDIATION"
    REPORT = "REPORT"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {State.COMPLETED, State.PARTIAL, State.FAILED, State.CANCELLED}
TRANSITIONS: dict[str, set[str]] = {
    State.RECEIVED: {State.TRIAGE},
    State.TRIAGE: {State.PLAN},
    State.PLAN: {State.COLLECT},
    State.COLLECT: {State.HYPOTHESIZE},
    State.HYPOTHESIZE: {State.VERIFY},
    State.VERIFY: {State.HYPOTHESIZE, State.PROPOSE_REMEDIATION, State.REPORT},
    State.PROPOSE_REMEDIATION: {State.WAITING_APPROVAL, State.REPORT},
    State.WAITING_APPROVAL: {State.EXECUTE_REMEDIATION, State.REPORT},
    State.EXECUTE_REMEDIATION: {State.VERIFY_REMEDIATION, State.REPORT},
    State.VERIFY_REMEDIATION: {State.REPORT},
    State.REPORT: {State.COMPLETED},
}


class IncidentInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    service: Service = "order-service"
    severity: Literal["low", "medium", "high"] = "high"
    scenario: str = Field("downstream_timeout", pattern=r"^[a-z0-9_]{1,64}$")
    request_remediation: bool = False


class Query(StrictModel):
    service: Service
    window_seconds: int = Field(300, ge=60, le=900)
    limit: int = Field(20, ge=1, le=50)
    cursor: int = Field(0, ge=0, le=200)
    template: Literal["health"] = "health"


class ToolRequest(StrictModel):
    name: ToolName
    args: Query


class Plan(StrictModel):
    tools: list[ToolRequest] = Field(min_length=1, max_length=6)
    rationale: str = Field(max_length=500)


class ToolOutput(StrictModel):
    source_type: str
    source_name: str
    records: list[dict[str, Any]] = Field(max_length=50)
    next_cursor: int | None = None
    trust_level: Literal["UNTRUSTED_DATA"] = "UNTRUSTED_DATA"


class EvidenceView(StrictModel):
    id: str
    source_type: str
    source_name: str
    query: dict[str, Any]
    observed_at: float
    summary: str
    content_hash: str
    payload_excerpt: list[dict[str, Any]]
    trust_level: Literal["UNTRUSTED_DATA"] = "UNTRUSTED_DATA"
    injection_flag: bool = False
    truncated: bool = False


class RootCause(StrictModel):
    cause: Cause
    affected_service: Service
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(max_length=12)
    reasoning_summary: str = Field(max_length=1000)
    falsification_test: str = Field(max_length=500)


class ActionProposal(StrictModel):
    name: Literal["disable_fault", "rollback_demo_version"]
    service: Service
    expected_revision: int = Field(ge=0)
    target_version: Literal["v1"] = "v1"


class HypothesisOutput(StrictModel):
    top_root_cause: RootCause
    alternatives: list[RootCause] = Field(default_factory=list, max_length=2)
    verification: ToolRequest | None = None
    proposed_action: ActionProposal | None = None


class Report(StrictModel):
    incident_id: str
    run_id: str
    summary: str
    top_root_cause: RootCause
    alternatives: list[RootCause]
    evidence: list[EvidenceView]
    ruled_out: list[str] = Field(default_factory=list)
    remediation: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float
    limitations: list[str]
    provider: str


class PolicyDecision(StrictModel):
    allowed: bool
    requires_approval: bool = True
    risk_level: Literal["HIGH"] = "HIGH"
    reasons: list[str]


class DecisionInput(StrictModel):
    reason: str = Field(min_length=3, max_length=500)


class AegisError(Exception):
    """Only safe category names, never remote response bodies, reach audit logs."""


class LeaseLost(AegisError):
    pass


class PolicyDenied(AegisError):
    pass


class BudgetExceeded(AegisError):
    pass


class InvalidEvidence(AegisError):
    pass


class RemoteFailure(AegisError):
    def __init__(self, category: str, retryable: bool = True) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable
