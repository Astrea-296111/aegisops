"""Exercise running local Docker demo services; never approves a remediation."""

import asyncio
import json
import time
from pathlib import Path

import httpx

from aegisops.config import Settings


async def main() -> None:
    cfg = Settings()
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for port in (8000, 8011, 8012):
            response = await client.get(f"http://127.0.0.1:{port}/health")
            response.raise_for_status()
        for port in (8011, 8012):
            response = await client.post(
                f"http://127.0.0.1:{port}/demo/fault",
                headers={"Authorization": "Bearer " + cfg.demo_token.get_secret_value()},
                json={"fault": "latency" if port == 8012 else "none", "version": "v1"},
            )
            response.raise_for_status()
        print("Generating a 20-second window of requests with an inventory latency fault...")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await client.get("http://127.0.0.1:8011/work")
            await asyncio.sleep(0.5)
        await asyncio.sleep(5)  # Give trace batching and metrics scraping time to export.
        headers = {"Authorization": "Bearer " + cfg.viewer_token.get_secret_value()}
        response = await client.post(
            "http://127.0.0.1:8000/incidents",
            headers=headers,
            json={"title": "Live demo checkout latency", "service": "order-service"},
        )
        response.raise_for_status()
        incident = response.json()
        response = await client.post(
            f"http://127.0.0.1:8000/incidents/{incident['id']}/investigate", headers=headers
        )
        response.raise_for_status()
        run_id = response.json()["id"]
        for _ in range(90):
            response = await client.get(f"http://127.0.0.1:8000/runs/{run_id}", headers=headers)
            response.raise_for_status()
            run = response.json()
            if run["state"] in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
                destination = Path("artifacts/local/live-report.json")
                await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    destination.write_text,
                    json.dumps(run, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(
                    json.dumps(
                        {"run_id": run_id, "state": run["state"], "output": str(destination)},
                        indent=2,
                    )
                )
                if run["state"] != "COMPLETED":
                    raise SystemExit(1)
                evidence = run["report"]["evidence"]
                present = {e["source_type"] for e in evidence if e["payload_excerpt"]}
                if not {"metrics", "logs", "traces"} <= present:
                    raise SystemExit(
                        "Report completed, but one telemetry source is empty. Inspect container logs and rerun."
                    )
                return
            await asyncio.sleep(1)
    raise SystemExit(
        "Investigation did not finish in 90 seconds; inspect docker compose logs worker."
    )


if __name__ == "__main__":
    asyncio.run(main())
