.PHONY: test py js e2e docker-test install-py deploy deploy-static deploy-detached deploy-status deploy-log

test: py js

py:
	uv run --frozen --group dev python -m pytest

js:
	npx vitest run

# Browser suite against a throwaway Docker node (never a live install).
e2e:
	scripts/test_e2e_docker.sh

# Build the image and exercise install, restart, and backup inside a container.
docker-test:
	scripts/test_docker_node.sh

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
