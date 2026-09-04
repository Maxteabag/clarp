"""Capability-gated generation of two durable portrait alternatives."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Callable

from . import (agent_portraits, agents, background_jobs, config, media_store,
               server_identity)
from .paths import RuntimePaths


MODEL = "gpt-image-2"
PROVIDER = "openai"
COUNT = 2
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_GENERATED_BYTES = 20 * 1024 * 1024
MAX_SOURCE_BYTES = 20 * 1024 * 1024
HEARTBEAT_TIMEOUT_MS = 5 * 60 * 1000
WORKER_ENV_NAMES = (
    "OPENAI_API_KEY", "CLAUDE_PWA_DB", "CLAUDE_PWA_CONFIG",
    "CLARP_MEDIA_DIR", "CLARP_CACHE_DIR", "CLARP_DATA_DIR",
    "CLARP_CONFIG_DIR", "CLARP_SHARE_DIR",
)
PROMPTS = (
    "Preserve the exact identity, facial features, hair, age, visual style, and "
    "character design from the reference. Create an intimate, warm close-up "
    "portrait that feels candid and personal, showing more of this character's "
    "life. Keep one character only. No text, logos, frames, or watermarks.",
    "Preserve the exact identity, facial features, hair, age, visual style, and "
    "character design from the reference. Create a natural everyday-life scene "
    "with the same character in a meaningful environment, emotionally warm and "
    "personal. Keep one character only. No text, logos, frames, or watermarks.",
)


class GenerationError(RuntimeError):
    pass


class GenerationCancelled(GenerationError):
    pass


def job_id_for(agent_id: str) -> str:
    return "portrait-generation-" + hashlib.sha256(agent_id.encode()).hexdigest()[:20]


def capability(session: str, *, media_dir: pathlib.Path | None = None) -> dict:
    agent = agents.get_by_session((session or "").strip())
    base = {
        "contract": "portrait-generation.v1", "available": False,
        "provider": PROVIDER, "model": MODEL, "count": COUNT,
        "cost_notice": "Uses the configured OpenAI API account; image charges may apply.",
        "reason": "", "job": None,
    }
    if not agent:
        return {**base, "reason": "Unknown Agent session"}
    job = background_jobs.get(job_id_for(agent["agent_id"]))
    base["job"] = job
    if not config.load().openai_key():
        return {**base, "reason": "Configure an OpenAI API key on this Computer"}
    try:
        collection = agent_portraits.list_for_session(
            agent["session"], portrait_dir=media_dir or _media_dir())
        primary_id = collection.get("primary_portrait_id")
        primary = agent_portraits.get_content(str(primary_id or ""))
        primary_path = pathlib.Path(str((primary or {}).get("storage_path") or ""))
        primary_mime = str((primary or {}).get("mime_type") or "")
        if not primary or not primary_path.is_file():
            return {**base, "reason": "Add or select a primary portrait first"}
        if primary_mime not in {"image/png", "image/jpeg", "image/webp"}:
            return {**base, "reason": "Use a PNG, JPEG, or WebP primary portrait"}
        if primary_path.stat().st_size > MAX_SOURCE_BYTES:
            return {**base, "reason": "Primary portrait is too large to generate from"}
    except agent_portraits.PortraitError as exc:
        return {**base, "reason": str(exc)}
    return {**base, "available": True}


def start(session: str, *, media_dir: pathlib.Path | None = None) -> dict:
    from .agent_lifecycle import AgentLifecycleService
    with AgentLifecycleService._lifecycle_gate.read():
        return _start(session, media_dir=media_dir)


def _start(session: str, *, media_dir: pathlib.Path | None = None) -> dict:
    session = (session or "").strip()
    agent = agents.get_by_session(session)
    if not agent:
        raise GenerationError("unknown Agent session")
    status = capability(session, media_dir=media_dir)
    if not status["available"]:
        raise GenerationError(str(status["reason"] or "portrait generation unavailable"))
    job_id = job_id_for(agent["agent_id"])
    existing = background_jobs.get(job_id)
    if existing and existing["status"] in background_jobs.ACTIVE_STATUSES:
        return {"job": existing, "capability": status}
    if existing and existing["status"] in background_jobs.TERMINAL_STATUSES:
        pid = int(existing.get("worker_pid") or 0)
        token = str(existing.get("worker_start_token") or "")
        if pid and token and background_jobs.worker_is_alive(pid, token):
            if not background_jobs.terminate_worker(pid, token):
                raise GenerationError("previous portrait generator is still stopping")
    computer_id = str(server_identity.get_server_info()["server_id"])
    job = background_jobs.upsert_computer(
        computer_id=computer_id, job_id=job_id, kind="portrait-generation",
        title=f"Generate portraits for {agent['persona']}",
        detail="Generating two character-consistent portrait alternatives",
        status="queued", heartbeat_timeout_ms=HEARTBEAT_TIMEOUT_MS,
        restart_cancelled=True,
        metadata={
            "session": session, "provider": PROVIDER, "model": MODEL,
            "expire_queued": True,
        },
    )
    if not background_jobs.claim_queued_launch(
        job_id, generation=int(job["generation"])):
        return {
            "job": background_jobs.get(job_id, reconcile=False),
            "capability": status,
        }
    command = [
        sys.executable, str(_worker_script()), "--handle",
        background_jobs.job_handle(job), "--session", session,
    ]
    try:
        if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
            subprocess.Popen(command, start_new_session=True, close_fds=True)
            launched = subprocess.CompletedProcess(command, 0, "", "")
        else:
            unit = f"clarp-portrait-{agent['agent_id'][:12]}-g{job['generation']}"
            from . import service_manager
            inherited_environment = {
                name: os.environ[name] for name in WORKER_ENV_NAMES
                if os.environ.get(name) is not None
            }
            ok, error = service_manager.launch_detached(
                command, unit=unit, environment=inherited_environment)
            launched = subprocess.CompletedProcess(
                command, 0 if ok else 1, "", error)
    except OSError as exc:
        launched = subprocess.CompletedProcess(command, 1, "", str(exc))
    if launched.returncode != 0:
        background_jobs.finish(
            job_id, generation=job["generation"], status="failed",
            reason="portrait_worker_launch_failed")
        raise GenerationError(launched.stderr.strip() or "could not start portrait generator")
    return {"job": background_jobs.get(job_id, reconcile=False), "capability": status}


def generate_two(
    session: str, *, handle: str, media_dir: pathlib.Path | None = None,
    should_continue: Callable[[], bool] = lambda: True,
    request: Callable[..., bytes] | None = None,
) -> dict:
    job_id, generation = background_jobs.parse_job_handle(handle)
    if not should_continue():
        raise GenerationCancelled("portrait generation cancelled")
    collection = agent_portraits.list_for_session(
        session, portrait_dir=media_dir or _media_dir())
    primary = agent_portraits.get_content(str(collection.get("primary_portrait_id") or ""))
    if not primary:
        raise GenerationError("primary portrait unavailable")
    source_path = pathlib.Path(str(primary.get("storage_path") or ""))
    if not source_path.is_file():
        raise GenerationError("primary portrait content unavailable")
    mime = str(primary.get("mime_type") or "image/png")
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise GenerationError("primary portrait format is not supported")
    if source_path.stat().st_size > MAX_SOURCE_BYTES:
        raise GenerationError("primary portrait exceeds the input size limit")
    with source_path.open("rb") as handle:
        source = handle.read(MAX_SOURCE_BYTES + 1)
    if len(source) > MAX_SOURCE_BYTES:
        raise GenerationError("primary portrait exceeds the input size limit")
    api_key = config.load().openai_key()
    if not api_key:
        raise GenerationError("OpenAI API key is not configured")
    requester = request or _request_edit
    generated = []
    for prompt in PROMPTS:
        if not should_continue():
            raise GenerationCancelled("portrait generation cancelled")
        raw = requester(api_key=api_key, source=source, source_mime=mime, prompt=prompt)
        if not raw or len(raw) > MAX_GENERATED_BYTES or agent_portraits._image_mime(raw) is None:
            raise GenerationError("image provider returned invalid portrait content")
        generated.append(raw)
    if len({hashlib.sha256(raw).digest() for raw in generated}) != COUNT:
        raise GenerationError("image provider returned duplicate portrait alternatives")
    if not should_continue():
        raise GenerationCancelled("portrait generation cancelled")
    assets = []
    try:
        for index, raw in enumerate(generated, start=1):
            generated_mime = agent_portraits._image_mime(raw)
            extension = {
                "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
            }[generated_mime]
            assets.append(media_store.publish(
                session=session, blob=raw,
                source_name=f"generated-portrait-{index}.{extension}",
                content_type=generated_mime,
                caption=f"Generated portrait alternative {index}",
                created_by=f"portrait_generation:{PROVIDER}",
                media_dir=media_dir or _media_dir(),
            ))
        return agent_portraits.replace_alternates_with_media_assets(
            session=session, asset_ids=[item["asset_id"] for item in assets],
            portrait_dir=media_dir or _media_dir(),
            expected_job=(job_id, generation),
        )
    except Exception:
        _discard_assets(assets)
        raise


def _discard_assets(assets: list[dict]) -> None:
    """Retire staged rows and remove only blobs with no remaining live owner."""
    if not assets:
        return
    from . import db
    con = db.conn()
    rows = [media_store.get(str(asset.get("asset_id") or "")) for asset in assets]
    paths = {str(row.get("storage_path") or "") for row in rows if row}
    con.execute("BEGIN IMMEDIATE")
    try:
        for asset in assets:
            con.execute(
                "UPDATE media_assets SET deleted_at=? WHERE asset_id=?",
                (db.now_ms(), asset["asset_id"]),)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    for raw_path in paths:
        if not raw_path:
            continue
        referenced = con.execute(
            """SELECT 1 FROM media_assets
                 WHERE storage_path=? AND deleted_at IS NULL
               UNION ALL
               SELECT 1 FROM agent_portraits
                 WHERE storage_path=? AND deleted_at IS NULL
               LIMIT 1""",
            (raw_path, raw_path),
        ).fetchone()
        if not referenced:
            try:
                pathlib.Path(raw_path).unlink()
            except FileNotFoundError:
                pass


def _request_edit(*, api_key: str, source: bytes, source_mime: str,
                  prompt: str, timeout: float = 180.0) -> bytes:
    boundary = "----clarp-" + secrets.token_hex(16)
    body = _multipart(boundary, {
        "model": MODEL, "prompt": prompt, "size": "1024x1024",
        "quality": "medium",
    }, source=source, source_mime=source_mime)
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/edits", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json", "User-Agent": "clarp-portrait-generation/1",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode(errors="replace")
        raise GenerationError(f"OpenAI image edit failed: HTTP {exc.code}: {detail}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise GenerationError("OpenAI image response exceeded the safety limit")
    try:
        data = json.loads(payload)
        encoded = data["data"][0]["b64_json"]
        return base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GenerationError("OpenAI image response was invalid") from exc


def _multipart(boundary: str, fields: dict[str, str], *, source: bytes,
               source_mime: str) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(), b"\r\n",
        ]
    chunks += [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image[]"; filename="portrait.png"\r\n',
        f"Content-Type: {source_mime}\r\n\r\n".encode(), source, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks)


def _worker_script() -> pathlib.Path:
    configured = os.environ.get("CLARP_PORTRAIT_GENERATION_WORKER", "").strip()
    if configured:
        return pathlib.Path(configured)
    if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
        return pathlib.Path(os.environ.get("CLARP_SHARE_DIR", "/opt/clarp")) / "scripts/portrait_generation_job.py"
    from . import xdg
    share = pathlib.Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir()))
    return share / "current/scripts/portrait_generation_job.py"


def _media_dir() -> pathlib.Path:
    return RuntimePaths.from_home(pathlib.Path.home()).media_dir
