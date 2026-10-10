# Same gates as .gitlab-ci.yml. On Windows without make, run the `uv run ...` commands directly
# (see "Developing on Windows" in the top-level README).
.PHONY: sync fmt lint typecheck test cov check db-up db-down

sync:
	uv sync

fmt:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:
	uv run pytest

# Coverage gate: >= 85% on domain, render and pipeline.
cov:
	uv run pytest --cov --cov-report=term-missing
	uv run coverage report --include="switchyard/domain/*,switchyard/render/*,switchyard/pipeline/*" --fail-under=85

check: lint typecheck cov

db-up:
	docker compose -f ../deploy/dev/compose.yaml up -d --wait

db-down:
	docker compose -f ../deploy/dev/compose.yaml down
