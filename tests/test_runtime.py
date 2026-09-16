import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from aegisops.domain import IncidentInput, LeaseLost, State
from aegisops.persistence.models import Step, ToolCall
from aegisops.persistence.repository import audit, transition
from aegisops.runtime.worker import Worker
from aegisops.security.guards import verify_chain


async def new_run(repo, **kwargs):
    incident = await repo.create_incident(IncidentInput(title="Checkout error", **kwargs))
    return await repo.start(incident.id)


@pytest.mark.parametrize(
    "scenario",
    [p.stem for p in (Path(__file__).parents[1] / "src/aegisops/data/scenarios").glob("*.json")],
)
async def test_replay_pipeline(repo, scenario):
    expected = json.loads((repo.cfg.fixtures_dir / (scenario + ".json")).read_text())[
        "ground_truth"
    ]
    run = await new_run(repo, scenario=scenario)
    result = await Worker(repo, repo.cfg).until_done(run.id)
    assert result["state"] == "COMPLETED"
    report = result["report"]
    assert report["top_root_cause"]["cause"] == expected["root_cause"]
    assert report["top_root_cause"]["affected_service"] == expected["affected_service"]
    assert verify_chain(await repo.events(run.id), result["event_head"])
    assert result["llm_calls"] <= repo.cfg.max_llm_calls


async def test_two_workers_cannot_claim_same_run(repo):
    run = await new_run(repo)
    claims = await asyncio.gather(repo.claim("A", run.id), repo.claim("B", run.id))
    assert sum(c is not None for c in claims) == 1


async def test_stale_fencing_token_rejected_and_recovery_audited(repo):
    run = await new_run(repo)
    old = await repo.claim("old", run.id)
    await Worker(repo, repo.cfg).begin(old)
    await asyncio.sleep(repo.cfg.lease_seconds + 0.02)
    current = await repo.claim("new", run.id)
    assert current.token > old.token
    with pytest.raises(LeaseLost):
        async with repo.locked(run.id, old) as (s, r, now):
            audit(s, r, "stale", "bad", {}, now)
    await repo.release(current)
    result = await Worker(repo, repo.cfg).until_done(run.id)
    assert result["state"] == "COMPLETED"
    events = await repo.events(run.id)
    assert any(e["event_type"] == "run.recovered" for e in events)
    assert not any(e["event_type"] == "bad" for e in events)


async def test_cancellation_at_safe_point(repo):
    run = await new_run(repo)
    worker = Worker(repo, repo.cfg)
    await worker.tick(run.id)
    await repo.cancel(run.id, "viewer")
    await worker.tick(run.id)
    assert (await repo.get_run(run.id)).state == "CANCELLED"


async def test_retry_is_bounded_and_audited(repo):
    run = await new_run(repo, scenario="noisy_429")
    result = await Worker(repo, repo.cfg).until_done(run.id)
    assert result["state"] == "COMPLETED"
    async with repo.db.sessions() as s:
        failed = (
            await s.scalars(
                select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.status == "failed")
            )
        ).all()
    assert {c.error_type for c in failed} == {"http_429", "http_5xx"}
    assert max(c.retry_count for c in failed) == 1


async def test_budget_yields_partial_report(repo):
    cfg = repo.cfg.model_copy(update={"max_tool_calls": 1})
    run = await new_run(repo)
    result = await Worker(repo, cfg).until_done(run.id)
    assert result["state"] == "PARTIAL"
    assert result["tool_calls"] == 1
    assert result["report"]["top_root_cause"]["cause"] == "insufficient_evidence"


async def test_transition_and_audit_rollback_together(repo):
    run = await new_run(repo)
    before = await repo.events(run.id)
    with pytest.raises(RuntimeError):
        async with repo.locked(run.id) as (s, r, now):
            transition(s, r, State.TRIAGE, "test", now)
            raise RuntimeError("injected crash before commit")
    assert (await repo.get_run(run.id)).state == "RECEIVED"
    assert await repo.events(run.id) == before


async def test_successful_tool_is_reused_after_interruption(repo):
    from aegisops.domain import Plan
    from aegisops.tools.registry import ToolRegistry
    from aegisops.tools.sources import ReplayDataSource

    run = await new_run(repo)
    worker = Worker(repo, repo.cfg)
    for _ in range(3):
        await worker.tick(run.id)
    lease = await repo.claim("crashed", run.id)
    step_id, _ = await worker.begin(lease)
    snapshot = await repo.get_run(run.id)
    request = Plan.model_validate(snapshot.context["plan"]).tools[0]
    registry = ToolRegistry(repo, ReplayDataSource(repo.cfg, "downstream_timeout"), repo.cfg)
    first = await registry.execute(lease, step_id, request, "collect")
    await asyncio.sleep(repo.cfg.lease_seconds + 0.02)
    result = await worker.until_done(run.id)
    assert result["state"] == "COMPLETED"
    assert first.id in {e["id"] for e in result["report"]["evidence"]}
    async with repo.db.sessions() as s:
        calls = (
            await s.scalars(
                select(ToolCall).where(
                    ToolCall.run_id == run.id, ToolCall.tool_name == "service_topology"
                )
            )
        ).all()
        steps = (await s.scalars(select(Step).where(Step.run_id == run.id))).all()
    assert len(calls) == 1
    assert any(s.status == "interrupted" for s in steps)


async def test_investigate_creation_is_idempotent(repo):
    incident = await repo.create_incident(IncidentInput(title="test"))
    a, b = await asyncio.gather(repo.start(incident.id), repo.start(incident.id))
    assert a.id == b.id
