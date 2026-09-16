from typing import Any

from pydantic import BaseModel

from aegisops.agent.context import build_context
from aegisops.domain import EvidenceView, IncidentInput, RemoteFailure
from aegisops.llm.providers import FakeDeterministicProvider
from aegisops.runtime.worker import Worker
from aegisops.security.guards import canonical, digest


def test_context_is_bounded_and_excludes_ground_truth():
    evidence = [
        EvidenceView(
            id=str(i),
            source_type="logs",
            source_name="loki",
            query={},
            observed_at=i,
            summary="x" * 200,
            content_hash=digest(i),
            payload_excerpt=[{"message": "y" * 4000}],
        )
        for i in range(20)
    ]
    result = build_context(
        {
            "title": "test",
            "service": "order-service",
            "severity": "high",
            "scenario": "secret-label",
            "ground_truth": "secret-answer",
        },
        evidence,
        2048,
    )
    content = canonical(result)
    assert len(content.encode()) <= 2048
    assert "secret-label" not in content and "secret-answer" not in content
    assert result["evidence"][-1]["id"] == "19"


class FailingProvider(FakeDeterministicProvider):
    async def generate(self, task: str, data: dict[str, Any], schema: type[BaseModel]):
        raise RemoteFailure("http_5xx")


async def test_permanent_provider_failure_stops_after_bounded_attempts(repo):
    incident = await repo.create_incident(IncidentInput(title="test"))
    run = await repo.start(incident.id)
    result = await Worker(repo, repo.cfg, FailingProvider()).until_done(run.id)
    assert result["state"] == "PARTIAL"
    assert result["llm_calls"] == 3


async def test_llm_call_budget_persists_before_provider_call(repo):
    cfg = repo.cfg.model_copy(update={"max_llm_calls": 1})
    incident = await repo.create_incident(IncidentInput(title="test"))
    run = await repo.start(incident.id)
    result = await Worker(repo, cfg).until_done(run.id)
    assert result["state"] == "PARTIAL"
    assert result["llm_calls"] == 1


async def test_token_budget_blocks_real_mode_before_network(repo):
    cfg = repo.cfg.model_copy(update={"provider": "openai", "max_tokens": 100})
    incident = await repo.create_incident(IncidentInput(title="test"))
    run = await repo.start(incident.id)
    result = await Worker(repo, cfg, FakeDeterministicProvider()).until_done(run.id)
    assert result["state"] == "PARTIAL" and result["llm_calls"] == 0


async def test_wall_budget_produces_partial_report(repo):
    incident = await repo.create_incident(IncidentInput(title="test"))
    run = await repo.start(incident.id)
    async with repo.locked(run.id) as (_s, current, now):
        current.created_at = now - 1000
    result = await Worker(repo, repo.cfg).until_done(run.id)
    assert result["state"] == "PARTIAL"
    assert result["report"]["limitations"]
