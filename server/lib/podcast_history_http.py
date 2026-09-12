"""Full-device, contextual podcast history API. No agent dispatch."""
import json
from urllib.parse import parse_qs, unquote, urlparse

from . import podcast_history


def handle(handler, method):
    if not (getattr(handler, "_request_auth_validated", False)
            and getattr(handler, "_request_device_scope", "") == "full"
            and getattr(handler, "_request_principal", "")):
        return handler._send(403, b'{"error":"Podcast history requires full-device authentication"}', "application/json")
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
                return handler._send(404, b'{"error":"Podcast conversation not found"}', "application/json")
        elif method == "POST" and suffix.endswith("/feedback"):
            data = handler._read_json()
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            value = podcast_history.feedback(suffix[:-len("/feedback")], data)
        else:
            return handler._send(404, b'{"error":"Not found"}', "application/json")
    except (ValueError, TypeError) as exc:
        return handler._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
    return handler._send(200, json.dumps(value).encode(), "application/json")
