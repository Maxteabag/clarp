#!/usr/bin/env bash
set -euo pipefail

IMAGE="${CLARP_TEST_IMAGE:-clarp:test}"
NAME="clarp-container-test-$$"
VOLUME="clarp-container-test-$$"
NETWORK="clarp-container-test-net-$$"
# shellcheck source=scripts/ci_docker_offline.sh
. "$(dirname "$0")/ci_docker_offline.sh"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    ci_offline_cleanup
}
trap cleanup EXIT

docker build -t "$IMAGE" .
# The container never gets a route out: an internal network, proven before the Host first starts.
ci_offline_create "$NETWORK"
ci_offline_preflight "$IMAGE"
mapfile -t OFFLINE < <(ci_offline_run_args)
docker volume create "$VOLUME" >/dev/null
docker run -d --name "$NAME" "${OFFLINE[@]}" \
    -v "$VOLUME:/data" "$IMAGE" >/dev/null
ci_offline_forward "$NAME"
BASE="$CI_OFFLINE_URL"

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
ci_offline_assert_inside "$NAME"

token="$(python3 scripts/pair_docker_test_client.py "$NAME" "$BASE")"
curl -fsS --noproxy '*' -H "Authorization: Bearer $token" \
    "$BASE/status" >/dev/null

server_info="$(curl -fsS --noproxy '*' -H "Authorization: Bearer $token" "$BASE/server-info")"
python3 -c '
import json, sys
sys.path.insert(0, "tests/contract")
import schema_check
info = json.loads(sys.argv[1])
schema = json.load(open("contract/schemas/server-info.json"))
schema_check.validate(info, schema)
version = str(info.get("clarp_version", ""))
assert version and version[0].isdigit(), f"invalid clarp_version: {version!r}"
' "$server_info"

server_id="$(docker exec "$NAME" sqlite3 /data/clarp/state.sqlite \
    "select value from settings where key='server_instance_id';")"
[[ -n "$server_id" ]]
docker exec "$NAME" python3 -c '
import sqlite3
db = sqlite3.connect("/data/clarp/state.sqlite")
rows = db.execute("""SELECT b.role,a.is_janitor FROM janitor_builtins b
    JOIN agents a ON a.agent_id=b.agent_id""").fetchall()
assert set(rows) == {("message-delegator",1),("tool-explainer",1),("audio-bookkeeper",1),("heartbeat-decider",1),("quota-monitor",1),("label-auditor",1)}, rows
assert db.execute("SELECT count(*) FROM agents WHERE is_janitor=0").fetchone()[0] == 0
assert db.execute("SELECT count(*) FROM runtimes").fetchone()[0] == 0
'
expected_skill_links="$(jq '[.skills[] | select(.pack == "core")] | length' \
    skills/manifest.json)"
[[ "$(docker exec "$NAME" sh -lc 'find /data/claude/skills -maxdepth 1 -type l | wc -l')" \
    == "$expected_skill_links" ]]
docker exec "$NAME" python3 -c 'import faster_whisper'
[[ "$(docker exec "$NAME" node --version)" == v22.23.4 ]]
[[ "$(docker exec "$NAME" npm --version)" == 12.0.2 ]]
[[ "$(docker exec "$NAME" npx --version)" == 12.0.2 ]]
docker exec "$NAME" sh -lc 'claude --version && codex --version'
docker exec "$NAME" clarp-admin doctor >/dev/null
docker exec "$NAME" sh -lc '
  command -v clarp-tui clarp-goal clarp-agent-tasks clarp-agent-artifacts clarp-media-publish \
    clarp-agent-bg clarp-github-workflow-artifact clarp-message-watch clarp-adopt >/dev/null
  clarp-agent-tasks show mike >/dev/null
  python3 -c "from lib import service_manager; ok,error=service_manager.launch_detached([\"/bin/true\"],unit=\"clarp-smoke\"); assert ok, error"
'
# Exercise the installed goal alias and managed skill in this disposable node.
docker exec "$NAME" python3 -c '
import json,subprocess
from lib import agents
agents.create_agent(persona="Goal smoke",voice_id="",cwd="/data/workspace",session="goal-smoke")
def call(*args): return json.loads(subprocess.check_output(["clarp-goal",*args],text=True))
p=call("create","goal-smoke","smoke","Verify installed helper","[]",json.dumps({"criteria":["CLI and working context function"],"limits":"No execution or publication","enroll":False}))
p=call("checkpoint",p["plan_id"],str(p["revision"]),json.dumps({"progress":"Installed goal helper works","next_work":"Complete verification","evidence":{"criterion-1":"This disposable CLI probe passed"},"documents":[{"name":"notes.md","format":"markdown","content":"# Verified installed helper","document_revision":0,"reason":"Smoke evidence"}]}))
assert call("read",p["plan_id"],"notes.md")["revision"]==1
assert call("complete",p["plan_id"],str(p["revision"]))["completion_verified"] is True
'
onboard="$(docker exec "$NAME" clarp-admin onboard --url "$BASE")"
[[ "$onboard" == clarp://pair* ]]
docker exec "$NAME" sh -lc 'mkdir -p /data/workspace/probe && printf preserved >/data/workspace/probe/marker'

docker rm -f "$NAME" >/dev/null
docker run -d --name "$NAME" "${OFFLINE[@]}" \
    -v "$VOLUME:/data" "$IMAGE" >/dev/null
ci_offline_forward "$NAME"
BASE="$CI_OFFLINE_URL"
for _ in $(seq 1 60); do
    [[ "$(docker inspect "$NAME" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')" == healthy ]] && break
    sleep 1
done
ci_offline_assert_inside "$NAME"
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
ci_offline_assert_inside "$NAME"
[[ "$(docker exec "$NAME" cat /data/workspace/probe/marker)" == preserved ]]

echo "docker node integration ok"
