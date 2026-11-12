.PHONY: test test-critical test-guards py js e2e docker-test isolated-codex install-py deploy deploy-static deploy-detached deploy-status deploy-log

test: py js

# The pre-push gate: only what can brick the Host or lose data (tests/critical.txt).
# Serial on purpose, so forty agents running it do not each take every core.
test-critical:
	uv run --frozen --group dev python -m pytest -n0 $$(sed -e 's/#.*//' tests/critical.txt)

# The structural guards, also before pushing: caught exceptions that are not
# logged, import layers and cycles, module state, backend capabilities. New
# code trips them more often than it trips the critical list. Serial.
GUARDS = tests/unit/test_swallowed_excepts.py tests/unit/test_import_layers.py \
         tests/unit/test_module_import_cycles.py tests/unit/test_module_state_guard.py \
         tests/unit/test_backend_capabilities.py
test-guards:
	uv run --frozen --group dev python -m pytest -n0 $(GUARDS)

py:
	uv run --frozen --group dev python -m pytest

js:
	uv run --frozen --group dev npm test

# Browser suite against a throwaway Docker node (never a live install).
e2e:
	scripts/test_e2e_docker.sh

# Build the image and exercise install, restart, and backup inside a container.
docker-test:
	scripts/test_docker_node.sh

# Fake Codex writer locks + Clarp source checks. No live auth, no network.
isolated-codex:
	tests/qa/run_isolated_docker.sh

build:
	npm run build

install-py:
	uv sync --frozen

deploy:
	./install.sh

# Client-only changes (CSS / HTML / static JS): sync into the running
# release without restarting the service. No server code is copied and
# in-flight agent turns are left alone. Not a substitute for `deploy` —
# see scripts/deploy_static.sh for the caveats.
deploy-static:
	scripts/deploy_static.sh

deploy-detached:
	scripts/deploy_detached.sh start

deploy-status:
	scripts/deploy_detached.sh status

deploy-log:
	scripts/deploy_detached.sh log
