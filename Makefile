# The repository's commands. Settings come from .env and the tracked
# configuration (metadata/catalog.yaml, catalog.publish.yaml); AGENTS.md
# explains each step.

RUN := uv run --env-file .env
DOCS := .cache/glace-docs

.DEFAULT_GOAL := help
.PHONY: help render check build finalize conformance publish-plan publish conformance-live links-live

help:  ## List the targets.
	@awk -F ':.*## ' '/^[a-z-]+:.*## / {printf "  %-17s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

render:  ## Render catalog/ from metadata/ and check it.
	uv run scripts/render-catalog.py
	uv run tests/test_render.py

check:  ## Run every gate.
	uv run tests/run_all.py

build:  ## Build one year into the store: make build YEAR=2027 [ARGS=...].
	@test -n "$(YEAR)" || { echo "usage: make build YEAR=2027 [ARGS=...]"; exit 2; }
	$(RUN) scripts/build-year.py $(YEAR) $(ARGS)

finalize:  ## Write the collections, indexes, styles and thumbnails over every year.
	$(RUN) scripts/finalize-catalog.py $(ARGS)

conformance:  ## Check the built store's documents with rashid.
	rm -rf $(DOCS)
	$(RUN) tools/mirror_docs.py $(DOCS)
	uv run rashid check $(DOCS) --schema --no-data --summary

publish-plan:  ## List what publishing would upload, and why.
	$(RUN) tools/publish.py

publish:  ## Upload the store to Source Cooperative.
	$(RUN) tools/publish.py --confirm

conformance-live:  ## Check the published catalog, its collection assets and its server.
	rm -rf $(DOCS)
	$(RUN) tools/mirror_docs.py $(DOCS)
	uv run rashid check $(DOCS) --schema --data --live

links-live:  ## Request every published href and compare its size with the store's.
	rm -rf $(DOCS)
	$(RUN) tools/mirror_docs.py $(DOCS)
	uv run tests/test_links.py --remote $(DOCS) $(ARGS)
