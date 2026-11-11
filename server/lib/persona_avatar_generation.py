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

from . import background_jobs, config, db, personas, roster, server_identity
from .log import log, log_exception


MODEL = "gpt-image-2"
PROVIDER = "openai"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_GENERATED_BYTES = 20 * 1024 * 1024
AVATAR_MAX_BYTES = 512_000
HEARTBEAT_TIMEOUT_MS = 5 * 60 * 1000
WORKER_ENV_NAMES = (
    "OPENAI_API_KEY", "CLAUDE_PWA_DB", "CLAUDE_PWA_CONFIG",
    "CLARP_MEDIA_DIR", "CLARP_CACHE_DIR", "CLARP_DATA_DIR",
    "CLARP_CONFIG_DIR", "CLARP_SHARE_DIR",
)

_HUMAN_VARIETY = (
    "a woman in her late twenties with warm dark eyes and wavy chestnut hair, wearing a soft oatmeal knit crew-neck",
    "a man in his early thirties with short curls, light stubble, and a relaxed smile, wearing an olive linen overshirt",
    "a South Asian woman in her thirties with expressive dark eyes and shoulder-length black hair, wearing a clean slate studio jacket",
    "an East Asian man in his late twenties with neat tousled hair and wire-rimmed glasses, wearing a charcoal merino sweater",
    "a woman in her mid-thirties with freckles, copper-auburn hair tied loosely back, wearing a warm navy cotton shirt",
    "a Black man in his thirties with a close-cropped fade and warm confident expression, wearing a minimalist sand overshirt",
    "a Mediterranean man in his late twenties with dark wavy hair and warm hazel eyes, wearing a stone-grey henley",
    "a Nordic woman in her early thirties with windswept blonde bob hair and bright blue-grey eyes, wearing a forest-green wool top",
)

_ANDROID_VARIETY = (
    "brushed warm titanium cranial plates, soft amber ring-lit optical irises, and a matte charcoal composite collar",
    "porcelain-white ceramic faceplates with delicate gold micro-seams, calm cyan optical irises, and a slate acoustic collar",
    "anodized slate-blue alloy cheek seams, warm silver-blue synthetic optics, and sculpted carbon-weave shoulder armour",
    "satin bronze-and-obsidian facial geometry, luminous seafoam optical sensors, and precision telemetry ear vanes",
)

_GROK_VARIETY = (
    "messy hair with electric magenta highlights, leather moto jacket, neon rain reflections behind",
    "undercut silver-streaked hair, vintage denim vest with patches, amber alley neon bokeh behind",
    "tousled jet-black hair with cyan tips, scarred brow and wry grin, dark synthwave rooftop lights behind",
    "cropped crimson-tinted hair, aviator collar jacket and confident smirk, industrial neon glow behind",
)

_GEMINI_VARIETY = (
    "luminous indigo stardust skin, warm golden eyes, and a floating crystalline Gemini spark star",
    "soft celestial violet-and-amber aura, joyful eyes, and swirling twilight nebula light",
    "warm sunlit cloud-and-gold genie aesthetic, friendly grin, and shimmering astral particles",
    "turquoise-and-starlight cosmic sage aesthetic, gentle expression, and radiant constellation bokeh",
)

_DEEPSEEK_VARIETY = (
    "matte charcoal technical dive collar, cold electric-blue bioluminescence on damp hair, drifting abyssal plankton",
    "brushed brass-and-slate submersible suit collar, cyan deep-sea glow across cheek, distant whale silhouette in dark water",
    "navy expedition drysuit collar with silver whale pin, soft aquamarine underwater light, tranquil deep-ocean bokeh",
    "darktitanium oceanographer collar, bioluminescent teal rim light and tiny suspended water droplets",
)

TIER_TEMPLATES: dict[str, str] = {
    "claude": (
        "A photorealistic candid 35mm portrait photograph of {name}, {detail}. "
        "Captured at a natural three-quarter angle in a sunlit architectural studio with soft natural bokeh. "
        "Authentic human skin texture, warm editorial window lighting, centered 1:1 head-and-shoulders framing, "
        "one person only, no CGI plastic sheen, no text, no logos, no borders, no watermarks."
    ),
    "codex": (
        "A photorealistic candid 35mm portrait photograph of {name}, a sleek humanoid android engineer featuring {detail}. "
        "Captured at a three-quarter angle in a calm modern workshop with soft ambient bokeh. "
        "Subtle cybernetic micro-seams and expressive synthetic optics, natural cinematic lighting, centered 1:1 head-and-shoulders framing, "
        "one character only, no green matrix rain, no text, no logos, no borders, no watermarks."
    ),
    "grok": (
        "Bold graphic-novel character portrait illustration of {name}, an edgy cyberpunk rebel engineer with {detail}. "
        "Expressive ink linework and painterly neon rim lighting, centered 1:1 head-and-shoulders framing, "
        "one character only, no text, no speech bubbles, no logos, no borders, no watermarks."
    ),
    "gemini": (
        "Stylized high-craft digital character portrait of {name}, a warm whimsical cosmic genie spirit with {detail}. "
        "Majestic celestial lighting, expressive friendly face, centered 1:1 head-and-shoulders framing, "
        "one character only, no text, no logos, no borders, no watermarks."
    ),
    "deepseek": (
        "Cinematic underwater portrait photograph of {name}, a calm deep-sea explorer with {detail}. "
        "Realistic skin texture, shallow depth of field, centered 1:1 head-and-shoulders framing, no goggles covering the eyes, "
        "one person only, no text, no logos, no borders, no watermarks."
    ),
}


class PersonaAvatarError(RuntimeError):
    pass


class PersonaAvatarCancelled(PersonaAvatarError):
    pass


def job_id_for(persona_id: str) -> str:
    return "persona-avatar-" + hashlib.sha256(persona_id.encode()).hexdigest()[:20]


def prompt_for_persona(name: str, *, tier: str = "", backend: str = "") -> str:
    resolved_tier = (
        (tier or "").strip().casefold()
        or roster.tier_for_contact(name)
        or roster.backend_to_tier(backend)
    )
    if resolved_tier not in TIER_TEMPLATES:
        resolved_tier = "claude"
    seed = int.from_bytes(hashlib.sha256(name.strip().casefold().encode()).digest()[:4], "big")
    varieties = {
        "claude": _HUMAN_VARIETY,
        "codex": _ANDROID_VARIETY,
        "grok": _GROK_VARIETY,
        "gemini": _GEMINI_VARIETY,
        "deepseek": _DEEPSEEK_VARIETY,
    }[resolved_tier]
    detail = varieties[seed % len(varieties)]
    return TIER_TEMPLATES[resolved_tier].format(name=name.strip(), detail=detail)


def start_best_effort(persona_name: str, *, tier: str = "", backend: str = "") -> dict | None:
    """Queue a detached background avatar generation job if OpenAI is configured.

    Never blocks session startup and never raises into the caller.
    """
    try:
        row = personas.get((persona_name or "").strip())
        if not row or row.get("builtin"):
            return None
        if row.get("avatar_path") or row.get("avatar_symbol"):
            return None
        if not config.load().openai_key():
            return None
        persona_id = str(row["persona_id"])
        resolved_tier = (
            (tier or "").strip().casefold()
            or roster.tier_for_contact(row["name"])
            or roster.backend_to_tier(backend)
        )
        job_id = job_id_for(persona_id)
        existing = background_jobs.get(job_id)
        if existing and existing["status"] in background_jobs.ACTIVE_STATUSES:
            return existing
        if existing and existing["status"] in background_jobs.TERMINAL_STATUSES:
            pid = int(existing.get("worker_pid") or 0)
            token = str(existing.get("worker_start_token") or "")
            if pid and token and background_jobs.worker_is_alive(pid, token):
                if not background_jobs.terminate_worker(pid, token):
                    return None
        computer_id = str(server_identity.get_server_info()["server_id"])
        job = background_jobs.upsert_computer(
            computer_id=computer_id,
            job_id=job_id,
            kind="persona-avatar",
            title=f"Generate avatar for {row['name']}",
            detail=f"Generating {resolved_tier} Contact portrait",
            status="queued",
            heartbeat_timeout_ms=HEARTBEAT_TIMEOUT_MS,
            restart_cancelled=True,
            metadata={
                "persona_id": persona_id,
                "persona_name": row["name"],
                "tier": resolved_tier,
                "provider": PROVIDER,
                "model": MODEL,
                "expire_queued": True,
            },
        )
        if not background_jobs.claim_queued_launch(
            job_id, generation=int(job["generation"])
        ):
            return background_jobs.get(job_id, reconcile=False)
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
    if row.get("avatar_path") or row.get("avatar_symbol"):
        return row
    api_key = config.load().openai_key()
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
    if latest.get("avatar_path") or latest.get("avatar_symbol"):
        return latest
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    updated = personas.update_avatar(
        original_name=latest["name"],
        avatar_symbol="",
        avatar_base64=encoded,
    )
    avatar_path = str(updated.get("avatar_path") or "")
    if avatar_path:
        con = db.conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """UPDATE agents
                      SET avatar_path = ?
                    WHERE persona = ? COLLATE NOCASE
                      AND COALESCE(avatar_path, '') = ''
                      AND COALESCE(avatar_symbol, '') = ''""",
                (avatar_path, updated["name"]),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
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
