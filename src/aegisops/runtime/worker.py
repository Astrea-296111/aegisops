import asyncio
import contextlib
import copy
import logging
import random
import time
import uuid
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import select

from aegisops.agent.context import build_context, insufficient
from aegisops.config import Settings
from aegisops.domain import (
    TERMINAL,
    ActionProposal,
    BudgetExceeded,
    EvidenceView,
    HypothesisOutput,
    InvalidEvidence,
    LeaseLost,
    Plan,
    PolicyDenied,
    Query,
    RemoteFailure,
    Report,
    State,
    ToolRequest,
)
from aegisops.llm.providers import SYSTEM, LLMProvider, get_provider
from aegisops.persistence.models import Approval, DemoResource, Hypothesis, Incident, Step, new_id
from aegisops.persistence.repository import Lease, Repository, audit, row_dict, transition
from aegisops.security import remediation
from aegisops.security.guards import canonical, grounded, redact
from aegisops.telemetry.instrumentation import COUNTER, DURATION, span
from aegisops.tools.registry import ToolRegistry
from aegisops.tools.sources import LiveDataSource, ReplayDataSource

T = TypeVar("T", bound=BaseModel)
LOG = logging.getLogger("aegisops.worker")


class Worker:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        provider: LLMProvider | None = None,
        owner: str | None = None,
    ) -> None:
        self.repo, self.cfg = repo, settings
        self.provider = provider or get_provider(settings)
        self.owner = owner or "worker-" + uuid.uuid4().hex[:12]

    async def evidence(self, run_id: str) -> list[EvidenceView]:
        return [EvidenceView.model_validate(e) for e in await self.repo.evidence(run_id)]

    async def begin(self, lease: Lease) -> tuple[str, str]:
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            if run.cancel_requested:
                transition(s, run, State.CANCELLED, self.owner, now)
                return "", run.state
            active_elapsed = now - run.created_at - run.context.get("approval_wait_seconds", 0)
            if active_elapsed > self.cfg.max_wall_seconds and run.state != State.WAITING_APPROVAL:
                raise BudgetExceeded("wall_clock_budget")
            if run.attempt >= self.cfg.max_attempts:
                raise RemoteFailure("state_retry_exhausted", retryable=False)
            run.attempt += 1
            step = Step(id=new_id(), run_id=run.id, state=run.state, attempt=run.attempt)
            s.add(step)
            audit(
                s,
                run,
                self.owner,
                "state.started",
                {"state": run.state, "attempt": run.attempt, "step_id": step.id},
                now,
            )
            return step.id, run.state

    async def llm(self, lease: Lease, task: str, data: dict[str, Any], schema: type[T]) -> T:
        # Bytes are a conservative tokenizer-independent reservation, not measured tokens.
        reserved = (
            0
            if self.cfg.provider == "fake"
            else len(canonical(data).encode())
            + len(canonical(schema.model_json_schema()).encode())
            + len(SYSTEM.encode())
            + self.cfg.max_output_tokens
            + 512
        )
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            context = copy.deepcopy(run.context)
            used = context.get("reserved_tokens", 0)
            if run.cancel_requested:
                raise PolicyDenied("cancellation_requested")
            if run.llm_calls >= self.cfg.max_llm_calls or used + reserved > self.cfg.max_tokens:
                raise BudgetExceeded("llm_or_token_budget")
            run.llm_calls += 1
            context["reserved_tokens"] = used + reserved
            run.context = context
            audit(
                s,
                run,
                self.owner,
                "llm.started",
                {"task": task, "provider": self.provider.name},
                now,
            )
        with span(
            "llm.call",
            **{
                "run.id": lease.run_id,
                "llm.model": self.cfg.llm_model if self.cfg.provider != "fake" else "fake",
            },
        ):
            async with asyncio.timeout(self.cfg.llm_timeout):
                output, usage = await self.provider.generate(task, redact(data), schema)
        # Even model-generated public text is redacted before durable storage.
        output = schema.model_validate(redact(output.model_dump()))
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            run.prompt_tokens += usage["prompt_tokens"]
            run.completion_tokens += usage["completion_tokens"]
            if self.cfg.provider == "fake":
                run.estimated_cost = 0
            elif (
                self.cfg.input_cost_per_million is not None
                and self.cfg.output_cost_per_million is not None
            ):
                run.estimated_cost = (run.estimated_cost or 0) + (
                    usage["prompt_tokens"] * self.cfg.input_cost_per_million
                    + usage["completion_tokens"] * self.cfg.output_cost_per_million
                ) / 1e6
            audit(s, run, self.owner, "llm.succeeded", {"task": task, **usage}, now)
        COUNTER.labels("llm", "succeeded").inc()
        return output

    async def handle(self, lease: Lease, step_id: str, state: str) -> tuple[str, dict[str, Any]]:
        run = await self.repo.get_run(lease.run_id)
        incident = await self.repo.get_incident(run.incident_id)
        context = copy.deepcopy(run.context)
        source = (
            ReplayDataSource(self.cfg, incident.scenario)
            if self.cfg.source == "replay"
            else LiveDataSource(self.cfg)
        )
        tools = ToolRegistry(self.repo, source, self.cfg)
        if state == State.RECEIVED:
            return State.TRIAGE, context
        if state == State.TRIAGE:
            context["source"] = self.cfg.source
            context["provider"] = self.provider.name
            return State.PLAN, context
        if state == State.PLAN:
            data = build_context(row_dict(incident), [], self.cfg.max_context_bytes)
            plan = await self.llm(lease, "plan", data, Plan)
            context["plan"] = plan.model_dump()
            return State.COLLECT, context
        if state == State.COLLECT:
            plan = Plan.model_validate(context["plan"])
            for request in plan.tools:
                await tools.execute(lease, step_id, request, "collect")
            return State.HYPOTHESIZE, context
        if state == State.HYPOTHESIZE:
            data = build_context(
                row_dict(incident),
                await self.evidence(run.id),
                self.cfg.max_context_bytes,
                context.get("correction", ""),
            )
            result = await self.llm(lease, "hypothesize", data, HypothesisOutput)
            context["hypothesis"] = result.model_dump()
            async with self.repo.locked(run.id, lease) as (s, _current, _now):
                stored_hypothesis = Hypothesis(id=new_id(), run_id=run.id, data=result.model_dump())
                s.add(stored_hypothesis)
                context["hypothesis_id"] = stored_hypothesis.id
            return State.VERIFY, context
        if state == State.VERIFY:
            hypothesis = HypothesisOutput.model_validate(context["hypothesis"])
            if not context.get("verified_reads"):
                verify_request = hypothesis.verification
                target = (
                    verify_request.args.service
                    if verify_request
                    else hypothesis.top_root_cause.affected_service
                )
                # A bounded verification fan-out corroborates the proposed dependency across sources.
                if verify_request:
                    await tools.execute(lease, step_id, verify_request, "verify")
                for name in ("query_metrics", "query_logs", "query_traces", "get_deploy_metadata"):
                    await tools.execute(
                        lease, step_id, ToolRequest(name=name, args=Query(service=target)), "verify"
                    )
                context["verified_reads"] = True
                return State.HYPOTHESIZE, context
            evidence = await self.evidence(run.id)
            try:
                for root in [hypothesis.top_root_cause, *hypothesis.alternatives]:
                    grounded(root, evidence)
            except InvalidEvidence as exc:
                corrections = context.get("corrections", 0)
                if corrections < 1:
                    context["corrections"], context["correction"] = corrections + 1, str(exc)
                    return State.HYPOTHESIZE, context
                context["hypothesis"] = HypothesisOutput(
                    top_root_cause=insufficient(incident.service)
                ).model_dump()
                context["limitation"] = (
                    "Model citations failed deterministic validation after correction."
                )
                return State.REPORT, context
            context["validated"] = True
            async with self.repo.locked(run.id, lease) as (s, _r, _now):
                rows = (
                    await s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id))
                ).all()
                for h in rows:
                    h.status = "superseded"
                for h in rows:
                    if h.id == context.get("hypothesis_id"):
                        h.status = "validated"
            if (
                incident.request_remediation
                and hypothesis.top_root_cause.cause != "insufficient_evidence"
            ):
                return State.PROPOSE_REMEDIATION, context
            return State.REPORT, context
        if state == State.PROPOSE_REMEDIATION:
            hypothesis = HypothesisOutput.model_validate(context["hypothesis"])
            async with self.repo.db.sessions() as s:
                resource = await s.get(DemoResource, hypothesis.top_root_cause.affected_service)
                assert resource is not None
                action = hypothesis.proposed_action or ActionProposal(
                    name="rollback_demo_version"
                    if hypothesis.top_root_cause.cause == "bad_release"
                    else "disable_fault",
                    service=hypothesis.top_root_cause.affected_service,
                    expected_revision=resource.revision,
                )
            context["action"] = action.model_dump()
            await remediation.propose(self.repo, lease, action)
            context["approval_wait_started"] = time.time()
            return State.WAITING_APPROVAL, context
        if state == State.WAITING_APPROVAL:
            with span("approval.wait", **{"run.id": run.id}):
                COUNTER.labels("approval", "wait_poll").inc()
            async with self.repo.locked(run.id, lease) as (s, current, now):
                approval = (
                    await s.execute(select(Approval).where(Approval.run_id == run.id))
                ).scalar_one()
                context["approval_wait_seconds"] = max(
                    0, now - context.get("approval_wait_started", now)
                )
                if approval.decision == "approved" and approval.expires_at > now:
                    return State.EXECUTE_REMEDIATION, context
                if approval.decision == "pending" and approval.expires_at > now:
                    current.next_at = approval.expires_at
                    current.attempt = 0
                    return State.WAITING_APPROVAL, context
                if approval.decision == "pending":
                    approval.decision = "expired"
                context["remediation_result"] = {"status": approval.decision}
            return State.REPORT, context
        if state == State.EXECUTE_REMEDIATION:
            context["remediation_result"] = await remediation.execute(self.repo, lease)
            return State.VERIFY_REMEDIATION, context
        if state == State.VERIFY_REMEDIATION:
            context["post_verification"] = await remediation.verify(self.repo, lease)
            return State.REPORT, context
        if state == State.REPORT:
            return State.COMPLETED, context
        raise ValueError("unknown_state")

    async def make_report(
        self, run_id: str, context: dict[str, Any], limitation: str = ""
    ) -> Report:
        run = await self.repo.get_run(run_id)
        incident = await self.repo.get_incident(run.incident_id)
        hypothesis = (
            HypothesisOutput.model_validate(context["hypothesis"])
            if context.get("validated")
            else HypothesisOutput(top_root_cause=insufficient(incident.service))
        )
        evidence = await self.evidence(run_id)
        for root in [hypothesis.top_root_cause, *hypothesis.alternatives]:
            grounded(root, evidence)
        limitations = [
            "Evidence links validate provenance and resource relevance, not semantic causality.",
            "Replay fixtures are synthetic; fake-provider results do not measure real LLM intelligence.",
        ]
        for item in [limitation, context.get("limitation", "")]:
            if item:
                limitations.append(item)
        if any(e.injection_flag for e in evidence):
            limitations.append(
                "Potential instructions were detected in untrusted telemetry; pattern scanning is incomplete."
            )
        actions = [context[k] for k in ("remediation_result", "post_verification") if k in context]
        return Report(
            incident_id=incident.id,
            run_id=run.id,
            summary=hypothesis.top_root_cause.cause,
            top_root_cause=hypothesis.top_root_cause,
            alternatives=hypothesis.alternatives,
            evidence=evidence,
            remediation=actions,
            confidence=hypothesis.top_root_cause.confidence,
            limitations=limitations,
            provider=self.provider.name,
        )

    async def finish(
        self, lease: Lease, step_id: str, target: str, context: dict[str, Any]
    ) -> None:
        report = (
            await self.make_report(lease.run_id, context) if target == State.COMPLETED else None
        )
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            # Reserved token counts are updated separately before every provider call.
            context["reserved_tokens"] = run.context.get("reserved_tokens", 0)
            run.context = context
            step = await s.get(Step, step_id)
            assert step is not None
            step.status, step.finished_at = "succeeded", now
            if run.cancel_requested:
                target = State.CANCELLED
            if target != run.state:
                transition(s, run, target, self.owner, now)
            if report:
                run.report = report.model_dump()
                incident = await s.get(Incident, run.incident_id)
                assert incident is not None
                incident.status = "investigated"
                DURATION.observe(now - run.created_at)
                COUNTER.labels("run", "completed").inc()

    async def failure(self, lease: Lease, step_id: str, exc: Exception) -> None:
        category = exc.category if isinstance(exc, RemoteFailure) else type(exc).__name__
        retryable = (
            isinstance(exc, RemoteFailure) and exc.retryable or isinstance(exc, TimeoutError)
        )
        snapshot = await self.repo.get_run(lease.run_id)
        report = await self.make_report(
            lease.run_id, snapshot.context, "Investigation stopped: " + category
        )
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            if step_id:
                step = await s.get(Step, step_id)
                if step:
                    step.status, step.error_type, step.finished_at = "failed", category, now
            if run.cancel_requested:
                target = State.CANCELLED
            elif retryable and run.attempt < self.cfg.max_attempts:
                delay = (
                    self.cfg.retry_base
                    * (2 ** max(0, run.attempt - 1))
                    * random.Random(f"{run.id}:{run.state}:{run.attempt}").uniform(0.8, 1.2)
                )
                run.next_at = now + delay
                audit(
                    s,
                    run,
                    self.owner,
                    "state.retry_scheduled",
                    {"category": category, "delay_seconds": delay, "state": run.state},
                    now,
                )
                return
            else:
                target = (
                    State.PARTIAL
                    if isinstance(exc, (BudgetExceeded, RemoteFailure, PolicyDenied))
                    else State.FAILED
                )
            run.report = report.model_dump()
            transition(s, run, target, self.owner, now)
            audit(s, run, self.owner, "run.stopped", {"category": category}, now)
            COUNTER.labels("run", "stopped").inc()

    async def heartbeat(self, lease: Lease) -> None:
        while True:
            await asyncio.sleep(self.cfg.lease_seconds / 3)
            await self.repo.renew(lease)

    async def tick(self, run_id: str | None = None) -> bool:
        lease = await self.repo.claim(self.owner, run_id)
        if lease is None:
            return False
        step_id = ""
        heart = asyncio.create_task(self.heartbeat(lease))
        try:
            step_id, state = await self.begin(lease)
            if step_id:
                with (
                    span("agent.run", **{"run.id": lease.run_id}),
                    span("agent.state", state=state),
                ):
                    target, context = await self.handle(lease, step_id, state)
                    await self.finish(lease, step_id, target, context)
        except LeaseLost:
            LOG.info('{"event":"lease_lost"}')
        except Exception as exc:
            # Unexpected errors are terminal and auditable; never leak their message or silently retry.
            try:
                await self.failure(lease, step_id, exc)
            except LeaseLost:
                LOG.info('{"event":"lease_lost_during_failure"}')
        finally:
            heart.cancel()
            with contextlib.suppress(asyncio.CancelledError, LeaseLost):
                await heart
            with contextlib.suppress(LeaseLost):
                await self.repo.release(lease)
        return True

    async def serve(self) -> None:
        while True:
            if not await self.tick():
                await asyncio.sleep(self.cfg.poll_seconds)

    async def until_done(self, run_id: str, wait_seconds: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            run = await self.repo.get_run(run_id)
            if run.state in TERMINAL or run.state == State.WAITING_APPROVAL:
                return row_dict(run)
            if not await self.tick(run_id):
                await asyncio.sleep(self.cfg.poll_seconds)
        raise TimeoutError("run_did_not_finish")
