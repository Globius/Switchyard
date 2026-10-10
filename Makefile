.PHONY: ui-install ui-lint ui-typecheck ui-test ui-build ui-gen-api ui-check

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

# Regenerate the typed client from docs/api/openapi.json.
ui-gen-api:
	cd ui && pnpm gen:api

# All UI gates, including "generated client matches the spec".
ui-check: ui-lint ui-typecheck ui-test ui-build
	cd ui && pnpm gen:api:check
