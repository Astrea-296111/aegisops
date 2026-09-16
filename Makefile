PYTHON ?= python
.PHONY: setup lint test up down migrate api worker demo demo-replay eval eval-smoke crash-recovery-demo
setup:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install --no-deps -e .
	$(PYTHON) -m aegisops init
lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy src
test:
	$(PYTHON) -m pytest -q
up:
	docker compose --profile live up -d --build
down:
	docker compose --profile live down
migrate:
	$(PYTHON) -m aegisops migrate
api:
	$(PYTHON) -m aegisops api
worker:
	$(PYTHON) -m aegisops worker
demo:
	$(PYTHON) scripts/live_demo.py
demo-replay:
	$(PYTHON) -m aegisops demo-replay
eval:
	$(PYTHON) -m aegisops eval
eval-smoke:
	$(PYTHON) -m aegisops eval-smoke
crash-recovery-demo:
	$(PYTHON) -m aegisops demo-crash-recovery

