.PHONY: ui-e2e ui-install ui-lint ui-typecheck ui-test ui-build ui-gen-api ui-check docs docs-serve ui-screenshots

# Frontend (ui/). Requires Node 22+ and pnpm. See docs/ui/README.md.
ui-install:
	cd ui && pnpm install --frozen-lockfile

ui-lint:
	cd ui && pnpm lint && pnpm format:check

ui-typecheck:
	cd ui && pnpm typecheck

ui-test:
	cd ui && pnpm test:cov

ui-build:
	cd ui && pnpm build

# Browser checks (axe, keyboard, phone width) against a mocked API. Needs Chrome: PW_CHANNEL=chrome, or
# `pnpm exec playwright install chromium` once.
ui-e2e:
	cd ui && pnpm e2e

# Regenerate the typed client from docs/api/openapi.json.
ui-gen-api:
	cd ui && pnpm gen:api

# All UI gates, including "generated client matches the spec".
ui-check: ui-lint ui-typecheck ui-test ui-build
	cd ui && pnpm gen:api:check
	cd ui && pnpm audit --audit-level high

# OpenTofu provider (Go): vet, tests, and "the generated table matches docs/api/openapi.json".
provider-check:
	cd provider && go vet ./... && go test ./...

# Docs site (MkDocs Material, ADR 0190): strict build with link checking into public/, same as CI.
# Needs uv only. On Windows without make: the same uvx command from the repo root.
docs:
	uvx --with-requirements docs/requirements.txt mkdocs build --strict

docs-serve:
	uvx --with-requirements docs/requirements.txt mkdocs serve

# Refresh the screenshots in docs/assets/screenshots (UI against a mocked API with a demo dataset).
ui-screenshots:
	cd ui && SY_SCREENSHOTS=1 pnpm exec playwright test screenshots --project desktop
