from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegisops.config import Settings
from aegisops.domain import TERMINAL, TRANSITIONS, IncidentInput, LeaseLost, PolicyDenied, State
from aegisops.persistence.database import Database, db_now
from aegisops.persistence.models import (
    Approval,
    AuditEvent,
    Base,
    DemoResource,
    Evidence,
    Incident,
    Run,
    Step,
    ToolCall,
)
from aegisops.security.guards import event_hash, redact


def row_dict(row: Base) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


@dataclass(frozen=True)
class Lease:
    run_id: str
    owner: str
    token: int


def audit(
    session: AsyncSession, run: Run, actor: str, kind: str, payload: dict[str, Any], now: float
) -> None:
    data = redact(payload)
    seq = run.event_seq + 1
    hashed = event_hash(run.id, seq, actor, kind, data, now, run.event_head)
    session.add(
        AuditEvent(
            run_id=run.id,
            sequence=seq,
            actor=actor,
            event_type=kind,
            payload=data,
            previous_hash=run.event_head,
            event_hash=hashed,
            created_at=now,
        )
    )
    run.event_seq = seq
    run.event_head = hashed


def transition(session: AsyncSession, run: Run, target: str, actor: str, now: float) -> None:
    if target not in TERMINAL and target not in TRANSITIONS.get(run.state, set()):
        raise ValueError(f"invalid transition {run.state} -> {target}")
    old = run.state
    run.state = target
    run.state_version += 1
    run.updated_at = now
    run.attempt = 0
    run.next_at = 0
    if target in TERMINAL:
        run.finished_at = now
    audit(
        session,
        run,
        actor,
        "state.transition",
        {"from": old, "to": target, "version": run.state_version},
        now,
    )


class Repository:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.db, self.cfg = database, settings

    @asynccontextmanager
    async def locked(
        self, run_id: str, lease: Lease | None = None
    ) -> AsyncIterator[tuple[AsyncSession, Run, float]]:
        async with self.db.sessions.begin() as session:
            now = await db_now(session)
            stmt = update(Run).where(Run.id == run_id)
            if lease:
                stmt = stmt.where(
                    Run.fencing_token == lease.token,
                    Run.lease_owner == lease.owner,
                    Run.lease_expires_at > now,
                )
            # A no-op UPDATE serializes mutations of a run, including its audit head.
            found = (
                await session.execute(stmt.values(id=Run.id).returning(Run.id))
            ).scalar_one_or_none()
            if found is None:
                if lease:
                    raise LeaseLost("stale_or_expired_lease")
                raise KeyError(run_id)
            run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            now = await db_now(session)
            if lease and run.lease_expires_at <= now:
                raise LeaseLost("expired_while_waiting_for_lock")
            yield session, run, now

    async def seed(self) -> None:
        async with self.db.sessions.begin() as s:
            # Migration creates rows. This is only for test create_all and clean local demos.
            for name in ("order-service", "inventory-service"):
                if await s.get(DemoResource, name) is None:
                    s.add(DemoResource(service=name))

    async def create_incident(self, value: IncidentInput) -> Incident:
        async with self.db.sessions.begin() as s:
            incident = Incident(**redact(value.model_dump()))
            s.add(incident)
            await s.flush()
            return incident

    async def start(self, incident_id: str) -> Run:
        async with self.db.sessions.begin() as s:
            # Lock the parent: concurrent investigate requests return the same run.
            result = await s.execute(
                update(Incident)
                .where(Incident.id == incident_id)
                .values(status="investigating")
                .returning(Incident.id)
            )
            if result.scalar_one_or_none() is None:
                raise KeyError(incident_id)
            existing = (
                await s.execute(select(Run).where(Run.incident_id == incident_id))
            ).scalar_one_or_none()
            if existing:
                return existing
            run = Run(incident_id=incident_id)
            s.add(run)
            await s.flush()
            audit(s, run, "api", "run.created", {}, await db_now(s))
            return run

    async def get_run(self, run_id: str) -> Run:
        async with self.db.sessions() as s:
            value = await s.get(Run, run_id)
            if value is None:
                raise KeyError(run_id)
            return value

    async def get_incident(self, incident_id: str) -> Incident:
        async with self.db.sessions() as s:
            value = await s.get(Incident, incident_id)
            if value is None:
                raise KeyError(incident_id)
            return value

    async def claim(self, owner: str, run_id: str | None = None) -> Lease | None:
        async with self.db.sessions.begin() as s:
            now = await db_now(s)
            eligible = (
                Run.state.not_in(list(TERMINAL)),
                Run.next_at <= now,
                Run.lease_expires_at <= now,
            )
            candidates = select(Run.id).where(*eligible).order_by(Run.created_at).limit(1)
            if run_id:
                candidates = candidates.where(Run.id == run_id)
            if s.bind is not None and s.bind.dialect.name == "postgresql":
                candidates = candidates.with_for_update(skip_locked=True)
            ident = (await s.execute(candidates)).scalar_one_or_none()
            if ident is None:
                return None
            result = await s.execute(
                update(Run)
                .where(Run.id == ident, *eligible)
                .values(
                    lease_owner=owner,
                    lease_expires_at=now + self.cfg.lease_seconds,
                    fencing_token=Run.fencing_token + 1,
                )
                .returning(Run.fencing_token)
            )
            token = result.scalar_one_or_none()
            if token is None:
                return None
            run = (await s.execute(select(Run).where(Run.id == ident))).scalar_one()
            orphan_steps = list(
                (
                    await s.scalars(
                        select(Step).where(Step.run_id == ident, Step.status == "running")
                    )
                ).all()
            )
            orphan_calls = list(
                (
                    await s.scalars(
                        select(ToolCall).where(
                            ToolCall.run_id == ident, ToolCall.status == "running"
                        )
                    )
                ).all()
            )
            for call in orphan_calls:
                call.status, call.finished_at, call.error_type = (
                    "interrupted",
                    now,
                    "worker_lost_read_may_repeat",
                )
            for step in orphan_steps:
                step.status, step.finished_at, step.error_type = "interrupted", now, "worker_lost"
            if orphan_steps:
                audit(
                    s,
                    run,
                    owner,
                    "run.recovered",
                    {"interrupted_steps": len(orphan_steps), "fencing_token": token},
                    now,
                )
            audit(s, run, owner, "lease.claimed", {"fencing_token": token}, now)
            return Lease(ident, owner, token)

    async def renew(self, lease: Lease) -> None:
        async with self.locked(lease.run_id, lease) as (_, run, now):
            run.lease_expires_at = now + self.cfg.lease_seconds

    async def release(self, lease: Lease) -> None:
        async with self.locked(lease.run_id, lease) as (_, run, _now):
            run.lease_owner, run.lease_expires_at = None, 0

    async def evidence(self, run_id: str) -> list[dict[str, Any]]:
        async with self.db.sessions() as s:
            return [
                e.data
                for e in (
                    await s.scalars(
                        select(Evidence).where(Evidence.run_id == run_id).order_by(Evidence.id)
                    )
                ).all()
            ]

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        await self.get_run(run_id)
        async with self.db.sessions() as s:
            return [
                row_dict(e)
                for e in (
                    await s.scalars(
                        select(AuditEvent)
                        .where(AuditEvent.run_id == run_id)
                        .order_by(AuditEvent.sequence)
                    )
                ).all()
            ]

    async def cancel(self, run_id: str, actor: str) -> dict[str, Any]:
        async with self.locked(run_id) as (s, run, now):
            if run.state in TERMINAL:
                return {"state": run.state, "cancel_requested": run.cancel_requested}
            run.cancel_requested = True
            run.next_at = 0
            audit(s, run, actor, "cancel.requested", {"state": run.state}, now)
            return {"state": run.state, "cancel_requested": True}

    async def approvals(self) -> list[dict[str, Any]]:
        async with self.db.sessions() as s:
            return [row_dict(a) for a in (await s.scalars(select(Approval).limit(200))).all()]

    async def decide(
        self, approval_id: str, role: str, decision: str, reason: str
    ) -> dict[str, Any]:
        if role != "operator":
            raise PolicyDenied("operator_required")
        async with self.db.sessions() as s:
            approval = await s.get(Approval, approval_id)
            if approval is None:
                raise KeyError(approval_id)
            run_id = approval.run_id
        async with self.locked(run_id) as (s, run, now):
            current = (
                await s.execute(select(Approval).where(Approval.id == approval_id))
            ).scalar_one()
            if (
                current.decision != "pending"
                or current.expires_at <= now
                or run.state != State.WAITING_APPROVAL
                or run.cancel_requested
            ):
                raise PolicyDenied("approval_not_actionable")
            current.decision, current.decided_by, current.reason = decision, role, redact(reason)
            run.next_at = 0
            audit(
                s,
                run,
                role,
                "approval.decided",
                {"approval_id": approval_id, "decision": decision, "reason": current.reason},
                now,
            )
            return row_dict(current)
