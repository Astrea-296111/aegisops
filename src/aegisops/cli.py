import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import uvicorn
from alembic import command
from alembic.config import Config

from aegisops.config import Settings
from aegisops.domain import IncidentInput, State
from aegisops.eval.runner import evaluate
from aegisops.persistence.database import Database
from aegisops.persistence.repository import Repository
from aegisops.runtime.worker import Worker
from aegisops.security.guards import verify_chain
from aegisops.telemetry.instrumentation import configure
from aegisops.tools.registry import ToolRegistry
from aegisops.tools.sources import ReplayDataSource


def initialize() -> None:
    path = Path(".env")
    from dotenv import dotenv_values, set_key

    if not path.exists():
        path.write_text(
            "AEGIS_DATABASE_URL=sqlite+aiosqlite:///./aegisops.db\nAEGIS_SOURCE=replay\nAEGIS_PROVIDER=fake\n",
            encoding="utf-8",
        )
    values = dotenv_values(path)
    for name in ("VIEWER", "OPERATOR", "DEMO"):
        key = f"AEGIS_{name}_TOKEN"
        if not values.get(key):
            set_key(path, key, secrets.token_urlsafe(32))
    migrate()
    print("Initialized. API credentials are in .env; do not commit or share that file.")


def migrate() -> None:
    config = Path("alembic.ini")
    if not config.exists():
        raise SystemExit(
            "Run this command from the extracted project directory (contains alembic.ini)."
        )
    command.upgrade(Config(str(config)), "head")


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def async_main(args: argparse.Namespace) -> None:
    cfg = Settings()
    if args.command in ("eval", "eval-smoke"):
        summary = await evaluate(cfg, Path(args.output), args.command == "eval-smoke")
        print(json.dumps(summary["methods"], indent=2))
        return
    if args.command == "demo-crash-recovery":
        cfg = cfg.model_copy(update={"lease_seconds": 0.5})
    db = Database(cfg.database_url)
    repo = Repository(db, cfg)
    worker = Worker(repo, cfg)
    configure(cfg.otlp_endpoint)
    try:
        if args.command == "worker":
            await worker.serve()
        elif args.command == "_crash-child":
            lease = await repo.claim("crash-child", args.run_id)
            assert lease is not None
            step_id, state = await worker.begin(lease)
            assert state == State.COLLECT
            from aegisops.domain import Plan

            run = await repo.get_run(args.run_id)
            incident = await repo.get_incident(run.incident_id)
            tools = ToolRegistry(repo, ReplayDataSource(cfg, incident.scenario), cfg)
            await tools.execute(
                lease, step_id, Plan.model_validate(run.context["plan"]).tools[0], "collect"
            )
            os._exit(
                17
            )  # Deliberate process death: no finally, close, lease release or checkpoint.
        elif args.command == "demo-crash-recovery":
            incident = await repo.create_incident(IncidentInput(title="Crash recovery demo"))
            run = await repo.start(incident.id)
            while (await repo.get_run(run.id)).state != State.COLLECT:
                await worker.tick(run.id)
            env = dict(os.environ, AEGIS_LEASE_SECONDS="0.5", AEGIS_DATABASE_URL=cfg.database_url)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "aegisops", "_crash-child", run.id, env=env
            )
            code = await proc.wait()
            if code != 17:
                raise RuntimeError(f"crash subprocess returned unexpected exit code {code}")
            result = await Worker(repo, cfg, owner="replacement-worker").until_done(run.id)
            events = await repo.events(run.id)
            recovered = any(e["event_type"] == "run.recovered" for e in events)
            valid = verify_chain(events, result["event_head"])
            assert result["state"] == State.COMPLETED and recovered and valid
            output = {
                "run_id": run.id,
                "child_exit_code": code,
                "state": result["state"],
                "recovered_event": recovered,
                "audit_chain_valid": valid,
            }
            save_json(Path("artifacts/local/crash-recovery.json"), output)
            save_json(Path("artifacts/local/crash-events.json"), events)
            print(json.dumps(output, indent=2))
        elif args.command == "audit":
            run = await repo.get_run(args.run_id)
            valid = verify_chain(await repo.events(run.id), run.event_head)
            print(json.dumps({"valid": valid, "run_id": run.id}))
            if not valid:
                raise SystemExit(1)
        elif args.command == "demo-replay":
            if cfg.source != "replay":
                raise SystemExit("demo-replay requires AEGIS_SOURCE=replay")
            incident = await repo.create_incident(
                IncidentInput(
                    title="Checkout availability alert",
                    scenario=args.scenario,
                    request_remediation=args.remediate,
                )
            )
            run = await repo.start(incident.id)
            result = await worker.until_done(run.id)
            folder = Path("artifacts/local") / run.id
            save_json(folder / "run.json", result)
            save_json(folder / "events.json", await repo.events(run.id))
            if result["report"]:
                save_json(folder / "report.json", result["report"])
            print(
                json.dumps(
                    {
                        "run_id": run.id,
                        "state": result["state"],
                        "root_cause": (result["report"] or {}).get("top_root_cause"),
                        "output": str(folder),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if result["state"] == State.WAITING_APPROVAL:
                print(
                    "Approve using the API as operator, then keep the worker running. CLI never auto-approves."
                )
            elif result["state"] != State.COMPLETED:
                raise SystemExit(1)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="aegisops")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "migrate", "worker", "demo-crash-recovery", "doctor"):
        sub.add_parser(name)
    api = sub.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    svc = sub.add_parser("demo-service")
    svc.add_argument("--service", choices=["order-service", "inventory-service"], required=True)
    svc.add_argument("--port", type=int, required=True)
    svc.add_argument("--host", default="127.0.0.1")
    demo = sub.add_parser("demo-replay")
    demo.add_argument("--scenario", default="downstream_timeout")
    demo.add_argument("--remediate", action="store_true")
    for name in ("eval", "eval-smoke"):
        p = sub.add_parser(name)
        p.add_argument(
            "--output",
            default="artifacts/eval/latest" if name == "eval" else "artifacts/eval/smoke",
        )
    for name in ("audit", "_crash-child"):
        sub.add_parser(name).add_argument("run_id")
    args = parser.parse_args()
    if args.command == "init":
        initialize()
    elif args.command == "migrate":
        migrate()
    elif args.command == "api":
        uvicorn.run("aegisops.api.app:create_app", factory=True, host=args.host, port=args.port)
    elif args.command == "demo-service":
        from aegisops.demo_service import create_demo_app

        uvicorn.run(create_demo_app(Settings(), args.service), host=args.host, port=args.port)
    elif args.command == "doctor":
        cfg = Settings()
        print(
            json.dumps(
                {
                    "python": sys.version.split()[0],
                    "database": cfg.database_url.split(":")[0],
                    "source": cfg.source,
                    "provider": cfg.provider,
                    "scenarios": len(list(cfg.fixtures_dir.glob("*.json"))),
                    "credentials_configured": bool(
                        cfg.viewer_token.get_secret_value()
                        and cfg.operator_token.get_secret_value()
                    ),
                },
                indent=2,
            )
        )
    else:
        try:
            asyncio.run(async_main(args))
        except KeyboardInterrupt:
            print("Stopped; a replacement worker can resume after lease expiry.")
