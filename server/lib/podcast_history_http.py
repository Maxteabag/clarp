"""Full-device, contextual podcast history API. No agent dispatch."""
from urllib.parse import parse_qs, unquote, urlparse

from . import podcast_history
from .http_utils import principal_of, require_full_scope, responder_of


def handle(handler, method):
    respond = responder_of(handler)
    denied = require_full_scope(principal_of(handler),
                                message="Podcast history requires full-device authentication")
    if denied:
        return respond.send_error(403, denied)
    parsed = urlparse(handler.path)
    query = parse_qs(parsed.query)
    param = lambda name, default="": query.get(name, [default])[0]
    suffix = unquote(parsed.path[len("/podcast-history"):].strip("/"))
    try:
        if method == "GET" and suffix == "sources":
            value = podcast_history.sources(text=param("search"), offset=int(param("offset", "0")), limit=int(param("limit", "50")))
        elif method == "GET" and not suffix:
            value = podcast_history.search(artifact_id=param("artifact_id"),
                source_artifact_id=param("source_artifact_id"), session=param("session"),
                text=param("search"), feedback_only=param("feedback_only") == "1",
                before=param("before"), limit=int(param("limit", "50")))
        elif method == "GET" and "/" not in suffix:
            value = podcast_history.get(suffix, after_event_id=int(param("after_event_id", "0")),
                through_event_id=int(param("through_event_id")) if param("through_event_id") else None,
                limit=int(param("limit", "500")))
            if value is None:
                return respond.send_error(404, "Podcast conversation not found")
        elif method == "POST" and suffix.endswith("/feedback"):
            data = respond.read_json()
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            value = podcast_history.feedback(suffix[:-len("/feedback")], data)
        else:
            return respond.send_error(404, "Not found")
    except (ValueError, TypeError) as exc:
        return respond.send_error(400, str(exc))
    return respond.send_json(200, value)
