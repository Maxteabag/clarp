"""Ordered route registry for server.py's Handler (refactor slice S3).

Each Route pairs a pure predicate on the bare request path (no query) with an
adapter: the body of the `if` branch that served the route in Handler.do_<METHOD>,
moved verbatim with `self` renamed `handler`. `dispatch` walks a method's routes
in their original order and runs the first adapter whose predicate matches; an
adapter returns NOT_HANDLED where the old chain fell through to the next branch.
The gate prelude (authorization, Oracle and device-scope checks) stays in
do_<METHOD> and runs before `dispatch`.

The exact route tables stay on Handler (_GET_ROUTES, _POST_ROUTES, _PUT_ROUTES)
and are consulted at the position they had in the chain. Behaviour is pinned by
tests/fixtures/route_goldens/ (dispatch.json and responses.json).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

NOT_HANDLED = object()


@dataclass(frozen=True)
class Route:
    method: str
    name: str
    match: Callable[[str], bool]
    adapter: Callable[[object, str], object]


# ---- adapters shared by several methods ---------------------------------

def _janitor(method):
    def adapter(handler, path):
        from lib import janitor_http
        return janitor_http.handle(handler, method)
    return adapter


def _janitor_handles(path):
    from lib import janitor_http
    return janitor_http.handles(path)


def _controller(method):
    def adapter(handler, path):
        from lib import controller_actions_http
        return controller_actions_http.handle(handler, method)
    return adapter


def _controller_handles(path):
    from lib import controller_actions_http
    return controller_actions_http.handles(path)


def _exact_table(attr):
    def adapter(handler, path):
        if handler._dispatch_exact(getattr(handler, attr), path):
            return None
        return NOT_HANDLED
    return adapter


def _always(path):
    return True


# ---- GET ----------------------------------------------------------------

def _get_root_static(handler, path):
    if path in handler._ROOT_STATIC:
        return handler._send_root_static(path)
    return NOT_HANDLED


def _get_podcast_history(handler, path):
    from lib import podcast_history_http
    return podcast_history_http.handle(handler, "GET")


def _get_team_messages(handler, path):
    team_id = path[len("/teams/"):-len("/messages")].strip("/")
    return handler._handle_team_messages(team_id)


def _get_background_job(handler, path):
    return handler._handle_background_job_detail(
        path[len("/background-jobs/"):].strip("/"))


def _get_artifact_html(handler, path):
    return handler._handle_artifact_html(unquote(path[len("/artifacts/"):-len("/html")].strip("/")))


def _get_artifact_events_config(handler, path):
    return handler._handle_artifact_events_config(unquote(path[len("/artifacts/"):-len("/events-config")].strip("/")), write=False)


def _get_artifact_events(handler, path):
    return handler._handle_artifact_events(unquote(path[len("/artifacts/"):-len("/events")].strip("/")), write=False)


def _get_artifact_revisions(handler, path):
    from lib import html_report_revisions
    artifact_id = unquote(path[len("/artifacts/"):-len("/revisions")].strip("/"))
    try:
        query = parse_qs(urlparse(handler.path).query)
        version = query.get("version", [None])[0]
        value = html_report_revisions.revisions(artifact_id, version,
            limit=int(query.get("limit", [50])[0]), offset=int(query.get("offset", [0])[0]))
    except (ValueError, TypeError) as exc:
        return handler._json_error(404, str(exc))
    return handler._json_ok({"revision" if version is not None else "revisions": value})


def _get_artifact(handler, path):
    return handler._handle_artifact_get(unquote(path[len("/artifacts/"):].strip("/")))


def _get_static(handler, path):
    rel = path[len("/static/"):]
    target = (handler.ctx.static / rel).resolve()
    if handler.ctx.static.resolve() in target.parents or target == handler.ctx.static.resolve():
        return handler._send_app_asset(target)
    return handler._send(403, b"forbidden")


def _get_media(handler, path):
    asset_id = path[len("/media/"):].strip("/")
    return handler._handle_media_content(asset_id)


def _get_portrait_content(handler, path):
    portrait_id = path[len("/agent-portraits/"):-len("/content")].strip("/")
    return handler._handle_agent_portrait_content(portrait_id)


def _get_notification_avatar(handler, path):
    return handler._handle_notification_avatar(
        path[len("/notification-avatars/"):].strip("/"))


def _get_owner_avatar(handler, path):
    from lib.viz_owner_avatar import portrait
    result=portrait(unquote(path[len("/viz/owner-avatar/"):]))
    return handler._send(200,*result) if result else handler._send(404,b"not found")


def _get_avatar(handler, path):
    return handler._handle_agent_avatar(path[len("/avatars/"):].strip("/"))


def _get_persona_avatar(handler, path):
    return handler._handle_persona_avatar(path[len("/persona-avatars/"):].strip("/"))


def _get_audio(handler, path):
    name = path[len("/audio/"):]
    target = (handler.ctx.audio_dir / name).resolve()
    if handler.ctx.audio_dir.resolve() in target.parents:
        # If the worker is still streaming bytes to this clip
        # (sidecar present, streamable=true, no `bytes` field yet),
        # serve with Transfer-Encoding: chunked so we don't send a
        # truncating Content-Length. iOS Safari falls back to
        # plain HTTP for audio/mpeg (MSE unsupported), and the
        # static handler's Content-Length=<partial> would stop
        # playback after the first few words.
        from lib.audio_growing import is_in_progress, serve_growing
        if target.is_file() and is_in_progress(target):
            return serve_growing(handler, target)
        return handler._send_file(target)
    return handler._send(403, b"forbidden")


def _get_stt_stream(handler, path):
    # Provider-owned turn taking: the phone streams PCM, the relay
    # forwards it to the chosen recogniser and returns its turn events.
    from lib.stt_stream import serve_stt_stream
    parsed = handler._query()
    return serve_stt_stream(
        handler, {k: (v[0] if v else "") for k, v in parsed.items()})


def _get_server_terminal(handler, path):
    if not getattr(handler, "_request_auth_validated", False):
        return handler._json_error(401, "Server terminal requires authenticated full-device access")
    if os.name != "posix":
        return handler._json_error(501, "Server terminal is unavailable on this platform")
    from lib.terminal_ws import serve_server_terminal
    return serve_server_terminal(handler)


def _get_terminal(handler, path):
    # Interactive terminal WS: spawns the agent's CLI in a PTY resumed
    # on the same session id and bridges raw bytes both ways.
    from lib.terminal_ws import serve_terminal
    session = path[len("/terminal/"):].strip("/")
    return serve_terminal(handler, session)


def _get_clip_stream(handler, path):
    raw = path[len("/clips/"):-len("/stream")].strip("/")
    try:
        clip_id = int(raw)
    except ValueError:
        return handler._send(400, b"bad clip id")
    from lib.clip_stream import serve_clip_stream
    return serve_clip_stream(
        handler, handler.ctx.clip_broker, clip_id, handler.ctx.audio_dir
    )


def _get_clip_complete(handler, path):
    raw = path[len("/clips/"):-len("/complete.mp3")].strip("/")
    try:
        clip_id = int(raw)
    except ValueError:
        return handler._send(400, b"bad clip id")
    return handler._send_complete_clip(clip_id)


def _get_clip_hls(handler, path):
    # HLS artifact routes: /clips/<id>/playlist.m3u8 and
    # /clips/<id>/segment-N.aac. Only present when HlsDelivery is the
    # active delivery; otherwise the dir just doesn't exist and we 404.
    rest = path[len("/clips/"):]
    parts = rest.split("/", 1)
    if len(parts) == 2:
        try:
            clip_id = int(parts[0])
        except ValueError:
            return handler._send(400, b"bad clip id")
        filename = parts[1]
        from lib.clip_delivery.hls import serve_hls_artifact
        return serve_hls_artifact(
            handler, handler.ctx.audio_dir, clip_id, filename,
        )
    return NOT_HANDLED


GET = (
    Route("GET", "janitor", _janitor_handles, _janitor("GET")),
    Route("GET", "controller-actions", _controller_handles, _controller("GET")),
    Route("GET", "root-static", _always, _get_root_static),
    Route("GET", "podcast-history",
          lambda path: path == "/podcast-history" or path.startswith("/podcast-history/"),
          _get_podcast_history),
    Route("GET", "exact", _always, _exact_table("_GET_ROUTES")),
    Route("GET", "team-messages",
          lambda path: path.startswith("/teams/") and path.endswith("/messages"),
          _get_team_messages),
    Route("GET", "background-job", lambda path: path.startswith("/background-jobs/"),
          _get_background_job),
    Route("GET", "artifact-html",
          lambda path: path.startswith("/artifacts/") and path.endswith("/html"),
          _get_artifact_html),
    Route("GET", "artifact-events-config",
          lambda path: path.startswith("/artifacts/") and path.endswith("/events-config"),
          _get_artifact_events_config),
    Route("GET", "artifact-events",
          lambda path: path.startswith("/artifacts/") and path.endswith("/events"),
          _get_artifact_events),
    Route("GET", "artifact-revisions",
          lambda path: path.startswith("/artifacts/") and path.endswith("/revisions"),
          _get_artifact_revisions),
    Route("GET", "artifact", lambda path: path.startswith("/artifacts/"), _get_artifact),
    Route("GET", "static", lambda path: path.startswith("/static/"), _get_static),
    Route("GET", "media", lambda path: path.startswith("/media/"), _get_media),
    Route("GET", "portrait-content",
          lambda path: path.startswith("/agent-portraits/") and path.endswith("/content"),
          _get_portrait_content),
    Route("GET", "notification-avatar", lambda path: path.startswith("/notification-avatars/"),
          _get_notification_avatar),
    Route("GET", "owner-avatar", lambda path: path.startswith("/viz/owner-avatar/"),
          _get_owner_avatar),
    Route("GET", "avatar", lambda path: path.startswith("/avatars/"), _get_avatar),
    Route("GET", "persona-avatar", lambda path: path.startswith("/persona-avatars/"),
          _get_persona_avatar),
    Route("GET", "audio", lambda path: path.startswith("/audio/"), _get_audio),
    Route("GET", "stt-stream", lambda path: path.startswith("/stt/stream"), _get_stt_stream),
    Route("GET", "server-terminal", lambda path: path == "/server-terminal",
          _get_server_terminal),
    Route("GET", "terminal", lambda path: path.startswith("/terminal/"), _get_terminal),
    Route("GET", "clip-stream",
          lambda path: path.startswith("/clips/") and path.endswith("/stream"),
          _get_clip_stream),
    Route("GET", "clip-complete",
          lambda path: path.startswith("/clips/") and path.endswith("/complete.mp3"),
          _get_clip_complete),
    Route("GET", "clip-hls",
          lambda path: path.startswith("/clips/") and "/" in path[len("/clips/"):],
          _get_clip_hls),
)


# ---- POST ---------------------------------------------------------------

def _post_turn_queue_send(handler, path):
    queue_id = path[len("/turn-queue/"):-len("/send")].strip("/")
    return handler._handle_turn_queue_send(queue_id)


def _post_agent_fork(handler, path):
    return handler._handle_agent_fork(
        unquote(path[len("/agents/"):-len("/fork")].strip("/")))


def _post_turn_queue_resume(handler, path):
    return handler._handle_turn_queue_resume()


def _post_team(handler, path):
    rest = path[len("/teams/"):].strip("/")
    if rest.endswith("/members"):
        return handler._handle_team_add_member(rest[:-len("/members")].strip("/"))
    return handler._handle_team_update(rest)


def _post_decision(handler, path):
    for action in ("resolve", "dismiss", "withdraw"):
        suffix = "/" + action
        if path.endswith(suffix):
            decision_id = unquote(path[len("/decisions/"):-len(suffix)].strip("/"))
            return getattr(handler, "_handle_decision_" + action)(decision_id)
    return NOT_HANDLED


def _post_podcast_history(handler, path):
    from lib import podcast_history_http
    return podcast_history_http.handle(handler, "POST")


def _post_artifact(handler, path):
    for action in ("archive", "discard", "submit", "pin", "events", "events-config"):
        suffix = "/" + action
        if path.endswith(suffix):
            artifact_id = unquote(path[len("/artifacts/"):-len(suffix)].strip("/"))
            return getattr(handler, "_handle_artifact_" + action.replace("-", "_"))(artifact_id)
    return handler._handle_artifact_update(unquote(path[len("/artifacts/"):].strip("/")))


POST = (
    Route("POST", "janitor", _janitor_handles, _janitor("POST")),
    Route("POST", "controller-actions", _controller_handles, _controller("POST")),
    Route("POST", "exact", _always, _exact_table("_POST_ROUTES")),
    Route("POST", "turn-queue-send",
          lambda path: path.startswith("/turn-queue/") and path.endswith("/send"),
          _post_turn_queue_send),
    Route("POST", "agent-fork",
          lambda path: path.startswith("/agents/") and path.endswith("/fork"),
          _post_agent_fork),
    Route("POST", "turn-queue-resume", lambda path: path == "/turn-queue/resume",
          _post_turn_queue_resume),
    Route("POST", "team", lambda path: path.startswith("/teams/"), _post_team),
    Route("POST", "decision", lambda path: path.startswith("/decisions/"), _post_decision),
    Route("POST", "podcast-history", lambda path: path.startswith("/podcast-history/"),
          _post_podcast_history),
    Route("POST", "artifact", lambda path: path.startswith("/artifacts/"), _post_artifact),
)


# ---- PUT ----------------------------------------------------------------

def _put_turn_queue(handler, path):
    return handler._handle_turn_queue_update(path[len("/turn-queue/"):].strip("/"))


PUT = (
    Route("PUT", "exact", _always, _exact_table("_PUT_ROUTES")),
    Route("PUT", "turn-queue", lambda path: path.startswith("/turn-queue/"), _put_turn_queue),
)


# ---- DELETE -------------------------------------------------------------

def _delete_team_member(handler, path):
    rest = path[len("/teams/"):]
    team_id, agent_id = rest.split("/members/", 1)
    return handler._handle_team_remove_member(team_id.strip("/"), agent_id.strip("/"))


def _delete_team(handler, path):
    return handler._handle_team_delete(path[len("/teams/"):].strip("/"))


def _delete_background_job(handler, path):
    return handler._handle_background_job_cancel(
        path[len("/background-jobs/"):].strip("/"))


def _delete_turn_queue(handler, path):
    return handler._handle_turn_queue_delete(path[len("/turn-queue/"):].strip("/"))


def _delete_transcription_result(handler, path):
    return handler._handle_transcription_result_delete(
        path[len("/transcription-results/"):].strip("/"))


def _delete_schedule(handler, path):
    return handler._handle_schedule_delete(path[len("/schedules/"):].strip("/"))


def _delete_live_activity(handler, path):
    return handler._handle_live_activity_delete()


def _delete_device(handler, path):
    return handler._handle_unregister_device()


def _delete_agent(handler, path):
    return handler._handle_delete_agent(path[len("/agents/"):])


def _delete_persona(handler, path):
    return handler._handle_delete_persona(path[len("/personas/"):].strip("/"))


DELETE = (
    Route("DELETE", "janitor", _janitor_handles, _janitor("DELETE")),
    Route("DELETE", "team-member",
          lambda path: path.startswith("/teams/") and "/members/" in path,
          _delete_team_member),
    Route("DELETE", "team", lambda path: path.startswith("/teams/"), _delete_team),
    Route("DELETE", "background-job", lambda path: path.startswith("/background-jobs/"),
          _delete_background_job),
    Route("DELETE", "turn-queue", lambda path: path.startswith("/turn-queue/"),
          _delete_turn_queue),
    Route("DELETE", "transcription-result",
          lambda path: path.startswith("/transcription-results/"),
          _delete_transcription_result),
    Route("DELETE", "schedule", lambda path: path.startswith("/schedules/"), _delete_schedule),
    Route("DELETE", "live-activity", lambda path: path == "/devices/live-activity",
          _delete_live_activity),
    Route("DELETE", "device", lambda path: path == "/devices", _delete_device),
    Route("DELETE", "agent", lambda path: path.startswith("/agents/"), _delete_agent),
    Route("DELETE", "persona", lambda path: path.startswith("/personas/"), _delete_persona),
)


# ---- PATCH --------------------------------------------------------------

def _patch_schedule(handler, path):
    return handler._handle_schedule_patch(path[len("/schedules/"):].strip("/"))


PATCH = (
    Route("PATCH", "schedule", lambda path: path.startswith("/schedules/"), _patch_schedule),
)


ROUTES: dict[str, tuple[Route, ...]] = {
    "GET": GET, "POST": POST, "PUT": PUT, "DELETE": DELETE, "PATCH": PATCH,
}


def dispatch(handler, method: str, path: str):
    """Serve `path` (bare, no query) with the first matching route, or 404."""
    for route in ROUTES[method]:
        if route.match(path):
            result = route.adapter(handler, path)
            if result is not NOT_HANDLED:
                return result
    return handler._send(404, b"not found")
