#!/usr/bin/env bash
set -euo pipefail

IMAGE="${CLARP_TEST_IMAGE:-clarp:test}"
NAME="clarp-container-test-$$"
VOLUME="clarp-container-test-$$"
PORT="${CLARP_TEST_PORT:-17692}"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t "$IMAGE" .
docker volume create "$VOLUME" >/dev/null
docker run -d --name "$NAME" \
    -p "127.0.0.1:${PORT}:7682" \
    -v "$VOLUME:/data" "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
    status="$(docker inspect "$NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')"
    [[ "$status" == healthy ]] && break
    [[ "$(docker inspect "$NAME" --format '{{.State.Running}}')" == true ]] || {
        docker logs "$NAME"
        exit 1
    }
    sleep 1
done
[[ "$(docker inspect "$NAME" --format '{{.State.Health.Status}}')" == healthy ]]

token="$(docker exec "$NAME" python3 -c \
    "import tomllib; print(tomllib.load(open('/data/clarp/config.toml','rb'))['server']['auth_token'])")"
curl -fsS -H "Authorization: Bearer $token" \
    "http://127.0.0.1:${PORT}/status" >/dev/null

server_id="$(docker exec "$NAME" sqlite3 /data/clarp/state.sqlite \
    "select value from settings where key='server_instance_id';")"
[[ -n "$server_id" ]]
[[ "$(docker exec "$NAME" sqlite3 /data/clarp/state.sqlite 'select count(*) from agents;')" == 0 ]]
expected_skill_links="$(jq '[.skills[] | select(.pack == "core")] | length' \
    skills/manifest.json)"
[[ "$(docker exec "$NAME" sh -lc 'find /data/claude/skills -maxdepth 1 -type l | wc -l')" \
    == "$expected_skill_links" ]]
docker exec "$NAME" python3 -c 'import faster_whisper'
[[ "$(docker exec "$NAME" node --version)" == v22.22.2 ]]
[[ "$(docker exec "$NAME" npm --version)" == 12.0.2 ]]
[[ "$(docker exec "$NAME" npx --version)" == 12.0.2 ]]
docker exec "$NAME" sh -lc 'claude --version && codex --version'
docker exec "$NAME" clarp-admin doctor >/dev/null
docker exec "$NAME" sh -lc '
  command -v clarp-tui clarp-agent-tasks clarp-agent-artifacts clarp-media-publish \
    clarp-agent-bg clarp-github-workflow-artifact clarp-message-watch >/dev/null
  clarp-agent-tasks show mike >/dev/null
  python3 -c "from lib import service_manager; ok,error=service_manager.launch_detached([\"/bin/true\"],unit=\"clarp-smoke\"); assert ok, error"
'
onboard="$(docker exec "$NAME" clarp-admin onboard --url "http://127.0.0.1:${PORT}")"
[[ "$onboard" == clarp://pair* ]]
docker exec "$NAME" sh -lc 'mkdir -p /data/workspace/probe && printf preserved >/data/workspace/probe/marker'

docker rm -f "$NAME" >/dev/null
docker run -d --name "$NAME" \
    -p "127.0.0.1:${PORT}:7682" \
    -v "$VOLUME:/data" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
    [[ "$(docker inspect "$NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')" == healthy ]] && break
    sleep 1
done
[[ "$(docker exec "$NAME" cat /data/workspace/probe/marker)" == preserved ]]
[[ "$(docker exec "$NAME" sqlite3 /data/clarp/state.sqlite \
    "select value from settings where key='server_instance_id';")" == "$server_id" ]]

backup="$(docker exec "$NAME" clarp-admin backup create | tail -1)"
docker exec "$NAME" clarp-admin backup verify "$backup" >/dev/null
docker exec "$NAME" sh -lc 'printf changed >/data/workspace/probe/marker'
docker exec "$NAME" clarp-admin backup restore "$backup" >/dev/null
docker restart "$NAME" >/dev/null
for _ in $(seq 1 60); do
    [[ "$(docker inspect "$NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')" == healthy ]] && break
    sleep 1
done
[[ "$(docker exec "$NAME" cat /data/workspace/probe/marker)" == preserved ]]

echo "docker node integration ok"
