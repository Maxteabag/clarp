"""Asynchronous detached profile-picture generation for Contacts (personas)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Callable

from . import agents as agents_db
from . import background_jobs, config, db, personas, roster, server_identity
from .log import log, log_exception


MODEL = "gpt-image-2"
PROVIDER = "openai"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_GENERATED_BYTES = 20 * 1024 * 1024
AVATAR_MAX_BYTES = 512_000
HEARTBEAT_TIMEOUT_MS = 5 * 60 * 1000
DAILY_WINDOW_MS = 24 * 60 * 60 * 1000
DEFAULT_DAILY_LIMIT = config.DEFAULT_CONTACT_AVATAR_DAILY_LIMIT
DEFAULT_MAX_ATTEMPTS = config.DEFAULT_CONTACT_AVATAR_MAX_ATTEMPTS
RETRY_BACKOFF_MS = (5 * 60 * 1000, 30 * 60 * 1000)
WORKER_ENV_NAMES = (
    "CLAUDE_PWA_DB", "CLAUDE_PWA_CONFIG",
    "CLARP_MEDIA_DIR", "CLARP_CACHE_DIR", "CLARP_DATA_DIR",
    "CLARP_CONFIG_DIR", "CLARP_SHARE_DIR",
)

class PersonaAvatarError(RuntimeError):
    pass


class PersonaAvatarCancelled(PersonaAvatarError):
    pass


def job_id_for(persona_id: str) -> str:
    return "persona-avatar-" + hashlib.sha256(persona_id.encode()).hexdigest()[:20]


def prompt_for_persona(name: str, *, tier: str = "", backend: str = "") -> str:
    spec = roster.contact_tier_spec(
        (tier or "").strip().casefold()
        or roster.tier_for_contact(name)
        or roster.backend_to_tier(backend)
    )
    seed = int.from_bytes(hashlib.sha256(name.strip().casefold().encode()).digest()[:4], "big")
    detail = spec.portrait_varieties[seed % len(spec.portrait_varieties)]
    return spec.portrait_template.format(name=name.strip(), detail=detail)


def start_best_effort(
    persona_name: str,
    *,
    tier: str = "",
    backend: str = "",
    now_ms: int | None = None,
) -> dict | None:
    """Queue a detached background avatar generation job if OpenAI is configured.

    Never blocks session startup and never raises into the caller.
    """
    try:
        row = personas.get((persona_name or "").strip())
        if not row or row.get("builtin"):
            return None
        if row.get("avatar_path") or row.get("avatar_symbol"):
            return None
        cfg = config.load()
        if not getattr(
            cfg,
            "contact_avatar_generation",
            config.DEFAULT_CONTACT_AVATAR_GENERATION,
        ):
            return None
        daily_limit = int(getattr(cfg, "contact_avatar_daily_limit", DEFAULT_DAILY_LIMIT))
        max_attempts = int(getattr(cfg, "contact_avatar_max_attempts", DEFAULT_MAX_ATTEMPTS))
        if daily_limit <= 0 or max_attempts <= 0:
            return None
        if not cfg.openai_key():
            return None
        now = int(now_ms if now_ms is not None else db.now_ms())
        persona_id = str(row["persona_id"])
        resolved_tier = (
            (tier or "").strip().casefold()
            or roster.tier_for_contact(row["name"])
            or roster.backend_to_tier(backend)
        )
        job_id = job_id_for(persona_id)
        background_jobs.reconcile_stale(job_id=job_id, now_ms=now)
        existing = background_jobs.get(job_id, reconcile=False, observed_at=now)
        if existing and existing["status"] in background_jobs.ACTIVE_STATUSES:
            return existing
        if existing and existing["status"] == "succeeded":
            return None
        if existing and existing["status"] in background_jobs.TERMINAL_STATUSES:
            attempts = int(existing.get("generation") or 1)
            if attempts >= max_attempts:
                return None
            backoff = RETRY_BACKOFF_MS[min(max(0, attempts - 1), len(RETRY_BACKOFF_MS) - 1)]
            ended_at = int(existing.get("terminal_at") or existing.get("updated_at") or 0)
            if ended_at and now - ended_at < backoff:
                return None
            pid = int(existing.get("worker_pid") or 0)
            token = str(existing.get("worker_start_token") or "")
            if pid and token and background_jobs.worker_is_alive(pid, token):
                if not background_jobs.terminate_worker(pid, token):
                    return None
        computer_id = str(server_identity.get_server_info()["server_id"])
        job, claimed = background_jobs.enqueue_computer_capped(
            computer_id=computer_id,
            job_id=job_id,
            kind="persona-avatar",
            title=f"Generate avatar for {row['name']}",
            detail=f"Generating {resolved_tier} Contact portrait",
            heartbeat_timeout_ms=HEARTBEAT_TIMEOUT_MS,
            restart_cancelled=True,
            max_launches=daily_limit,
            window_ms=DAILY_WINDOW_MS,
            max_attempts=max_attempts,
            retry_backoffs_ms=RETRY_BACKOFF_MS,
            now_ms=now,
            metadata={
                "persona_id": persona_id,
                "persona_name": row["name"],
                "tier": resolved_tier,
                "provider": PROVIDER,
                "model": MODEL,
                "expire_queued": True,
            },
        )
        if not job or not claimed:
            return job
        command = [
            sys.executable, str(_worker_script()),
            "--handle", background_jobs.job_handle(job),
            "--persona-id", persona_id,
            "--tier", resolved_tier,
        ]
        try:
            if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
                subprocess.Popen(command, start_new_session=True, close_fds=True)
                launched_ok, error = True, ""
            else:
                unit = f"clarp-persona-avatar-{persona_id[-12:]}-g{job['generation']}"
                from . import service_manager
                inherited_environment = {
                    name: os.environ[name] for name in WORKER_ENV_NAMES
                    if os.environ.get(name) is not None
                }
                launched_ok, error = service_manager.launch_detached(
                    command, unit=unit, environment=inherited_environment)
        except OSError as exc:
            launched_ok, error = False, str(exc)
        if not launched_ok:
            background_jobs.finish(
                job_id, generation=job["generation"], status="failed",
                reason=(error or "persona_avatar_worker_launch_failed")[:500])
            log("personaAvatarLaunchFail", f"{row['name']}: {error}")
            return None
        return background_jobs.get(job_id, reconcile=False)
    except Exception as exc:  # noqa: BLE001
        log_exception("personaAvatarStartFail", exc, detail=persona_name)
        return None


def generate_for_persona(
    persona_id: str,
    *,
    tier: str = "",
    handle: str = "",
    should_continue: Callable[[], bool] = lambda: True,
    request: Callable[..., bytes] | None = None,
) -> dict:
    if handle:
        background_jobs.parse_job_handle(handle)
    if not should_continue():
        raise PersonaAvatarCancelled("persona avatar generation cancelled")
    row = personas.get_by_id(persona_id)
    if not row:
        raise PersonaAvatarError("Contact no longer exists")
    if row.get("builtin") or row.get("avatar_path") or row.get("avatar_symbol"):
        return row
    cfg = config.load()
    if not getattr(
        cfg,
        "contact_avatar_generation",
        config.DEFAULT_CONTACT_AVATAR_GENERATION,
    ):
        raise PersonaAvatarCancelled("Contact avatar generation is disabled")
    api_key = cfg.openai_key()
    if not api_key:
        raise PersonaAvatarError("OpenAI API key is not configured")
    prompt = prompt_for_persona(row["name"], tier=tier)
    requester = request or _request_generation
    raw = requester(api_key=api_key, prompt=prompt)
    if not raw or len(raw) > MAX_GENERATED_BYTES:
        raise PersonaAvatarError("image provider returned empty or oversized content")
    if not should_continue():
        raise PersonaAvatarCancelled("persona avatar generation cancelled")
    jpeg_bytes = _normalize_avatar_jpeg(raw)
    if len(jpeg_bytes) > AVATAR_MAX_BYTES:
        raise PersonaAvatarError("generated avatar exceeds Contact size limit")
    if not should_continue():
        raise PersonaAvatarCancelled("persona avatar generation cancelled")
    latest = personas.get_by_id(persona_id)
    if not latest:
        raise PersonaAvatarError("Contact was removed before avatar save")
    if latest.get("builtin") or latest.get("avatar_path") or latest.get("avatar_symbol"):
        return latest
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    updated = personas.update_avatar(
        original_name=latest["name"],
        avatar_symbol="",
        avatar_base64=encoded,
    )
    avatar_path = str(updated.get("avatar_path") or "")
    if avatar_path:
        agents_db.sync_contact_avatar(updated["name"], avatar_path)
    return updated


def _normalize_avatar_jpeg(raw: bytes) -> bytes:
    """Center-crop to 1:1 and Lanczos-downsample to 512x512 JPEG <= 512 KB."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.TemporaryDirectory(prefix="clarp-avatar-") as tmp:
            tmp_dir = pathlib.Path(tmp)
            src = tmp_dir / "raw.img"
            dst = tmp_dir / "avatar.jpg"
            src.write_bytes(raw)
            for qscale in ("3", "6", "10"):
                proc = subprocess.run(
                    [
                        ffmpeg, "-y", "-loglevel", "error",
                        "-i", str(src),
                        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=512:512:flags=lanczos",
                        "-frames:v", "1",
                        "-q:v", qscale,
                        str(dst),
                    ],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                if proc.returncode == 0 and dst.is_file():
                    data = dst.read_bytes()
                    if 0 < len(data) <= AVATAR_MAX_BYTES:
                        return data
    if raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")) and len(raw) <= AVATAR_MAX_BYTES:
        return raw
    raise PersonaAvatarError("could not downsample generated avatar to <=512KB JPEG")


def _request_generation(*, api_key: str, prompt: str, timeout: float = 180.0) -> bytes:
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "size": "1024x1024",
        "quality": "medium",
        "output_format": "jpeg",
        "output_compression": 85,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "clarp-persona-avatar/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode(errors="replace")
        raise PersonaAvatarError(
            f"OpenAI avatar generation failed: HTTP {exc.code}: {detail}") from exc
    if len(payload) > MAX_RESPONSE_BYTES:
        raise PersonaAvatarError("OpenAI image response exceeded safety limit")
    try:
        data = json.loads(payload)
        encoded = data["data"][0]["b64_json"]
        return base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PersonaAvatarError("OpenAI image response was invalid") from exc


def _worker_script() -> pathlib.Path:
    configured = os.environ.get("CLARP_PERSONA_AVATAR_WORKER", "").strip()
    if configured:
        return pathlib.Path(configured)
    if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
        return pathlib.Path(os.environ.get("CLARP_SHARE_DIR", "/opt/clarp")) / "scripts/persona_avatar_job.py"
    from . import xdg
    share = pathlib.Path(os.environ.get("CLARP_SHARE_DIR", xdg.data_dir()))
    candidate = share / "current/scripts/persona_avatar_job.py"
    if candidate.is_file():
        return candidate
    repo_candidate = pathlib.Path(__file__).resolve().parents[2] / "scripts/persona_avatar_job.py"
    return repo_candidate if repo_candidate.is_file() else candidate
