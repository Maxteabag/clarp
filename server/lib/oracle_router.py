"""Bounded Luna routing through Responses or the Host's ChatGPT Codex login.

Both adapters return proposals to the existing AgentTools admission path. Codex
has no tools, workspace, hooks or skills. Subscription failure never falls back
to the metered API. No token, environment or provider error body is journaled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid
from urllib.request import Request, urlopen

from jsonschema import ValidationError, validate

MODEL = "gpt-5.6-luna"
BACKENDS = ("api", "codex")
MAX_RESULT_BYTES = 128000
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "multi_agent_v2", "browser_use", "computer_use",
    "image_generation", "view_image", "code_mode_host", "remote_plugin",
    "skill_search", "shell_snapshot", "goals", "sleep_tool",
)


class RouterError(RuntimeError):
    """A safe, stable failure code; never contains a provider response body."""


def subscription_environment():
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "CODEX_HOME",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
        "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def proposal_schema(tools):
    return {"type": "object", "properties": {
        "message": {"type": "string"},
        "action": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {
            "name": {"type": "string", "enum": [tool["name"] for tool in tools]},
            "arguments": {"type": "string", "description": "JSON object matching the selected tool's parameters"}},
            "required": ["name", "arguments"], "additionalProperties": False}]}},
        "required": ["message", "action"], "additionalProperties": False}


def validate_result(result, tools):
    """Validate all proposed actions before any of them can be admitted."""
    if not isinstance(result, dict) or not isinstance(result.get("output"), list):
        raise RouterError("invalid_router_output")
    definitions = {tool["name"]: tool["parameters"] for tool in tools}
    actions = 0
    has_message = False
    for item in result["output"]:
        if not isinstance(item, dict):
            raise RouterError("invalid_router_item")
        if item.get("type") == "function_call":
            actions += 1
            if actions > 1 or item.get("name") not in definitions:
                raise RouterError("invalid_router_action")
            if not isinstance(item.get("call_id"), str) or not item["call_id"]:
                raise RouterError("invalid_router_call_id")
            try:
                arguments = json.loads(item["arguments"])
                validate(arguments, definitions[item["name"]])
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                raise RouterError("invalid_router_arguments") from exc
        elif item.get("type") == "message":
            if not isinstance(item.get("content"), list):
                raise RouterError("invalid_router_message")
            for part in item["content"]:
                if (not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)):
                    raise RouterError("invalid_router_text")
                has_message = has_message or bool(part["text"].strip())
        elif item.get("type") != "reasoning":
            raise RouterError("unexpected_router_output")
    if not actions and not has_message:
        raise RouterError("empty_router_result")
    return result


def _terminate(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _codex(body, stop, timeout):
    started = time.monotonic()
    env = subscription_environment()
    # Check before forced_login_method: Codex logs out mismatching credentials.
    # A Host using API authentication must fail without changing its login.
    status = subprocess.run(["codex", "login", "status"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
    if status.returncode or b"Logged in using ChatGPT" not in status.stdout:
        raise RouterError("codex_chatgpt_login_required")
    if stop.is_set():
        raise RouterError("router_cancelled")
    login_ms = round((time.monotonic() - started)*1000, 1)
    with tempfile.TemporaryDirectory(prefix="clarp-oracle-router-") as directory:
        root = Path(directory)
        schema = proposal_schema(body["tools"])
        (root / "schema.json").write_text(json.dumps(schema))
        (root / "instructions.txt").write_text(body["instructions"] +
            "\nReturn one routing proposal matching the schema. Do not execute work yourself. "
            "Set action to null when answering or clarifying. When choosing an action, "
            "leave message empty; the tool result is not yet known. Tool contracts:\n" +
            json.dumps(body["tools"], ensure_ascii=False))
        args = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
            "--model", MODEL, "--output-schema", str(root / "schema.json"),
            "--output-last-message", str(root / "answer.json")]
        for setting in ['forced_login_method="chatgpt"', 'approval_policy="never"',
                        'web_search="disabled"', 'project_doc_max_bytes=0', 'mcp_servers={}',
                        'model_reasoning_effort="low"',
                        f'model_instructions_file={json.dumps(str(root / "instructions.txt"))}']:
            args.extend(["-c", setting])
        for feature in DISABLED_FEATURES:
            args.extend(["--disable", feature])
        args.extend(["--enable", "skip_host_skill_discovery", "-"])
        with (root / "events.jsonl").open("w+b") as events:
            process = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.PIPE,
                stdout=events, stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + timeout
            payload = body["input"].encode()
            try:
                while True:
                    if stop.is_set():
                        raise RouterError("router_cancelled")
                    if time.monotonic() >= deadline:
                        raise RouterError("router_timeout")
                    try:
                        process.communicate(payload, timeout=.1)
                        break
                    except subprocess.TimeoutExpired:
                        payload = None
                if process.returncode:
                    raise RouterError("codex_router_failed")
                answer = root / "answer.json"
                if not answer.is_file() or answer.stat().st_size > MAX_RESULT_BYTES:
                    raise RouterError("invalid_codex_result_size")
                proposal = json.loads(answer.read_text())
                validate(proposal, schema)
                output = []
                if proposal["action"]:
                    if proposal["message"].strip():
                        raise RouterError("unverified_action_commentary")
                    output.append({"type": "function_call", **proposal["action"],
                                   "call_id": "codex-" + uuid.uuid4().hex})
                elif proposal["message"].strip():
                    output.append({"type": "message", "content": [
                        {"type": "output_text", "text": proposal["message"]}]})
                usage = {}
                events.seek(0)
                for line in events.read(2*1024*1024).splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage") or {}
                return {"output": output, "usage": usage,
                        "transport_metrics": {"login_ms": login_ms,
                            "cli_ms": round((time.monotonic() - started)*1000-login_ms, 1)}}
            finally:
                _terminate(process)


def route(body, *, backend, api_key, stop, timeout=35):
    if backend not in BACKENDS:
        raise RouterError("unsupported_router_backend")
    if body.get("model") != MODEL:
        raise RouterError("unsupported_router_model")
    if stop.is_set():
        raise RouterError("router_cancelled")
    started = time.monotonic()
    try:
        if backend == "codex":
            result = _codex(body, stop, timeout)
        else:
            if not api_key:
                raise RouterError("api_key_required")
            request = Request("https://api.openai.com/v1/responses", json.dumps(body).encode(),
                {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                raise RouterError("oversized_router_result")
            result = json.loads(raw)
        if stop.is_set():
            raise RouterError("router_cancelled")
        validate_result(result, body["tools"])
        result["router"] = {"backend": backend, "model": MODEL,
            "billing": "chatgpt_subscription" if backend == "codex" else "openai_api",
            "elapsed_ms": round((time.monotonic() - started)*1000, 1)}
        return result
    except RouterError:
        raise
    except Exception as exc:
        raise RouterError("router_request_failed") from exc
