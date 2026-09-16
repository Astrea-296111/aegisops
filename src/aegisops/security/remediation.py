from typing import Any

from sqlalchemy import select, update

from aegisops.domain import ActionProposal, PolicyDenied, State
from aegisops.persistence.models import Approval, DemoResource, Execution
from aegisops.persistence.repository import Lease, Repository, audit, row_dict
from aegisops.security.guards import action_policy, digest
from aegisops.telemetry.instrumentation import COUNTER, span


async def propose(repo: Repository, lease: Lease, action: ActionProposal) -> None:
    async with repo.locked(lease.run_id, lease) as (s, run, now):
        resource = await s.get(DemoResource, action.service)
        if resource is None:
            raise PolicyDenied("resource_scope")
        with span("policy.evaluate", **{"run.id": run.id}):
            decision = action_policy(
                action,
                state=run.state,
                role="agent",
                approval=None,
                run_id=run.id,
                now=now,
                current_revision=resource.revision,
                cancelled=run.cancel_requested,
            )
        audit(s, run, lease.owner, "policy.proposal", decision.model_dump(), now)
        if not decision.allowed:
            raise PolicyDenied("proposal_denied")
        existing = (
            await s.execute(select(Approval).where(Approval.run_id == run.id))
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                Approval(
                    run_id=run.id,
                    proposed_action=action.model_dump(),
                    action_hash=digest(action.model_dump()),
                    expires_at=now + repo.cfg.approval_seconds,
                )
            )
            audit(
                s,
                run,
                "agent",
                "approval.requested",
                {"action": action.model_dump(), "risk": "HIGH"},
                now,
            )
            COUNTER.labels("approval", "requested").inc()


async def execute(repo: Repository, lease: Lease) -> dict[str, Any]:
    """Demo writes are DB-only: receipt, mutation and audit commit atomically."""
    async with repo.locked(lease.run_id, lease) as (s, run, now):
        approval = (
            await s.execute(select(Approval).where(Approval.run_id == run.id))
        ).scalar_one_or_none()
        if approval is None:
            raise PolicyDenied("approval_missing")
        action = ActionProposal.model_validate(run.context["action"])
        hashed = digest(action.model_dump())
        execution_id = digest({"run_id": run.id, "action_hash": hashed})
        receipt = await s.get(Execution, execution_id)
        if receipt is not None:
            if receipt.action_hash != hashed:
                raise PolicyDenied("receipt_mismatch")
            return receipt.result
        # Serializes different runs attempting to update the same resource revision.
        await s.execute(
            update(DemoResource)
            .where(DemoResource.service == action.service)
            .values(service=DemoResource.service)
        )
        resource = await s.get(DemoResource, action.service)
        if resource is None:
            raise PolicyDenied("unknown_resource")
        with span("policy.evaluate", **{"run.id": run.id}):
            decision = action_policy(
                action,
                state=run.state,
                role=approval.decided_by or "agent",
                approval=row_dict(approval),
                run_id=run.id,
                now=now,
                current_revision=resource.revision,
                cancelled=run.cancel_requested,
            )
        if not decision.allowed:
            raise PolicyDenied("execution_denied:" + ",".join(decision.reasons))
        with span("remediation.execute", **{"run.id": run.id, "risk.level": "HIGH"}):
            if action.name == "disable_fault":
                resource.fault = "none"
            elif action.name == "rollback_demo_version":
                resource.version = action.target_version
            resource.revision += 1
            resource.updated_at = now
            result = {
                "status": "succeeded",
                "action": action.model_dump(),
                "revision": resource.revision,
                "service": resource.service,
                "fault": resource.fault,
                "version": resource.version,
                "effect_scope": "demo_database_only",
                "execution_id": execution_id,
            }
            s.add(Execution(id=execution_id, run_id=run.id, action_hash=hashed, result=result))
            audit(s, run, "operator", "remediation.executed", result, now)
    COUNTER.labels("remediation", "succeeded").inc()
    return result


async def verify(repo: Repository, lease: Lease) -> dict[str, Any]:
    async with repo.locked(lease.run_id, lease) as (s, run, now):
        if run.state != State.VERIFY_REMEDIATION:
            raise PolicyDenied("invalid_verification_state")
        receipt = (
            await s.execute(select(Execution).where(Execution.run_id == run.id))
        ).scalar_one()
        resource = await s.get(DemoResource, receipt.result["service"])
        assert resource is not None
        action = ActionProposal.model_validate(receipt.result["action"])
        passed = resource.revision == receipt.result["revision"] and (
            resource.fault == "none"
            if action.name == "disable_fault"
            else resource.version == action.target_version
        )
        result = {
            "verified": passed,
            "check": "persisted_demo_configuration",
            "business_recovery": "NOT_MEASURED",
        }
        audit(s, run, lease.owner, "remediation.verified", result, now)
        return result
