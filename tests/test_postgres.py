import asyncio
import os

import pytest

from aegisops.config import Settings
from aegisops.domain import IncidentInput, LeaseLost
from aegisops.persistence.database import Database
from aegisops.persistence.repository import Repository
from aegisops.runtime.worker import Worker


@pytest.mark.postgres
async def test_postgres_claim_fence_and_pipeline():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL absent; PostgreSQL validation not claimed")
    cfg = Settings(_env_file=None, database_url=url, lease_seconds=0.3, poll_seconds=0.01)
    db = Database(url)
    await db.create_for_test()
    repo = Repository(db, cfg)
    await repo.seed()
    try:
        incident = await repo.create_incident(IncidentInput(title="Postgres integration"))
        run = await repo.start(incident.id)
        claims = await asyncio.gather(repo.claim("a", run.id), repo.claim("b", run.id))
        active = [c for c in claims if c is not None]
        assert len(active) == 1
        await asyncio.sleep(0.35)
        recovered = await repo.claim("new", run.id)
        with pytest.raises(LeaseLost):
            await repo.renew(active[0])
        await repo.release(recovered)
        result = await Worker(repo, cfg).until_done(run.id)
        assert result["state"] == "COMPLETED"
    finally:
        await db.close()
