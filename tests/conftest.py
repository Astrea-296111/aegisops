from collections.abc import AsyncIterator

import pytest

from aegisops.config import Settings
from aegisops.persistence.database import Database
from aegisops.persistence.repository import Repository


@pytest.fixture
async def repo(tmp_path) -> AsyncIterator[Repository]:
    cfg = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///" + str(tmp_path / "test.db"),
        retry_base=0.001,
        poll_seconds=0.01,
        lease_seconds=0.3,
        viewer_token="viewer-test-token-longer-than-24",
        operator_token="operator-test-token-longer-than-24",
    )
    database = Database(cfg.database_url)
    await database.create_for_test()
    result = Repository(database, cfg)
    await result.seed()
    yield result
    await database.close()
