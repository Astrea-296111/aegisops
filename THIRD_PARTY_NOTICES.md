# Third-party notices

AegisOps application code is provided under the MIT license in LICENSE.
Design references are attributed in docs/design-notes.md; their source code has
not been copied into this application.

The Windows bundle contains unmodified wheel distributions in wheelhouse/.
Each wheel retains its original dist-info metadata, license and notice files.
Those dependencies retain their own licenses; the AegisOps MIT license does
not relicense them. A metadata-derived inventory is in docs/dependency-licenses.md.
The archive wheelhouse/SHA256SUMS.txt records the exact bundled bytes.

Primary dependencies include FastAPI, Pydantic, SQLAlchemy, HTTPX, Alembic,
aiosqlite, asyncpg, Uvicorn, OpenTelemetry and prometheus-client. Development
dependencies include pytest, pytest-asyncio, Ruff, mypy, setuptools and wheel.

Docker Compose references separately distributed images for PostgreSQL,
Prometheus, Grafana Loki, Grafana Tempo, OpenTelemetry Collector and Python.
These images are not included in the archive and have their own licenses and
notices. In particular, Grafana Loki and Tempo are AGPL-3.0 projects. Consult
the exact upstream image and repository notices before redistributing images.

The optional HTML documentation builder uses Python-Markdown (BSD-3-Clause).
The generated HTML has no external scripts or fonts; rebuilding is optional.

