# clipwarden: every command the README mentions. Run `make help`.
SHELL := /bin/bash
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
CW := $(BIN)/python -m clipwarden.cli
export PYTHONPATH := $(CURDIR)/src

.PHONY: help setup demo dashboard test lint sandbox sandbox-test sample assets media check site compose-init compose-up secrets clean

help: ## list commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-13s %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)

setup: $(BIN)/python ## create .venv and install hash-pinned dependencies
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet --require-hashes -r requirements-dev.lock
	@echo "ready: source $(VENV)/bin/activate"

demo: ## run the whole pipeline on the bundled CC0 sample (< 1 minute)
	$(BIN)/python scripts/demo.py

dashboard: ## open the review dashboard for the demo (http://127.0.0.1:8765)
	$(CW) --config demo-data/clipwarden.toml dashboard

test: ## unit, gate, security, equation and end-to-end tests
	$(BIN)/python -m pytest -q

lint: ## ruff (style + bandit security rules)
	$(BIN)/ruff check src scripts tests

sandbox: ## build the network-less FFmpeg sandbox image
	docker build -f sandbox/Dockerfile.media -t clipwarden-media:0.1.0 .

sandbox-test: ## prove the sandbox is isolated (no network, read-only, non-root)
	bash scripts/sandbox_smoke.sh clipwarden-media:0.1.0
	CLIPWARDEN_TEST_IMAGE=clipwarden-media:0.1.0 $(BIN)/python -m pytest -q tests/test_sandbox_and_web.py -k hostile

sample: ## regenerate the CC0 sample talk (needs espeak-ng)
	$(BIN)/python scripts/make_sample.py

assets: ## regenerate vector visuals (+ README flowchart)
	$(BIN)/python scripts/render_assets.py

media: ## regenerate hero GIF, frame composites, dashboard screenshots (after `make demo`)
	$(BIN)/python scripts/render_media.py
	$(BIN)/python scripts/export_site.py

check: lint test ## everything CI runs locally
	$(BIN)/python scripts/render_assets.py --check
	$(BIN)/python scripts/check_refs.py

site: ## preview the GitHub Pages site at http://127.0.0.1:8000
	rm -rf _site && mkdir -p _site/assets && cp -r site/. _site/ && cp -r assets/. _site/assets/ && rm -rf _site/assets/src
	cd _site && $(BIN)/python -m http.server 8000 --bind 127.0.0.1

secrets: ## create secrets/vault_passphrase.txt for docker compose (random, chmod 600)
	@mkdir -p secrets && umask 077 && $(PY) -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/vault_passphrase.txt
	@echo "wrote secrets/vault_passphrase.txt (keep a copy in your password manager)"

compose-init: secrets ## prepare ./data for docker compose
	@mkdir -p data/jobs && chmod 777 data/jobs
	@[ -f data/clipwarden.toml ] || cp config/clipwarden.compose.toml data/clipwarden.toml
	@echo "edit data/clipwarden.toml, then: make compose-up"

compose-up: ## build and start app + media containers
	APP_UID=$$(id -u) APP_GID=$$(id -g) docker compose up -d --build
	@echo "dashboard: http://127.0.0.1:8765  (first-run password: docker compose logs app)"

clean: ## remove demo output and caches (keeps your data/)
	rm -rf demo-data _site .pytest_cache .ruff_cache
