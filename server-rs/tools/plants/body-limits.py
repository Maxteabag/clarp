#!/usr/bin/env python3
"""Wrong R81 transport rules, exercised through HTTP, the relay and an agent's tool."""
import runner

EDGE = 'clarp-host/src/edge.rs'
RUNS = {'host': ['test', '--test', 'host', '--', 'the_host_accepts_full_voice_batches_and_large_artifact_bodies']}
CHANGES = [
    ('the common limit loses the last byte', '16 * 1024 * 1024;', '16 * 1024 * 1024 - 1;'),
    ('the common limit admits another byte', '16 * 1024 * 1024;', '16 * 1024 * 1024 + 1;'),
    ('pairing refuses its last byte', '64 * 1024;', '64 * 1024 - 1;'),
    ('pairing admits another byte', '64 * 1024;', '64 * 1024 + 1;'),
    ('pairing has the general allowance', '(&Method::POST, EXCHANGE) => PAIRING_BODY_LIMIT', '(&Method::POST, EXCHANGE) => JSON_BODY_LIMIT'),
    ('an import is capped like a regular send', '=> IMPORT_LIMIT,', '=> JSON_BODY_LIMIT,'),
    ('a PUT bypasses the policy', 'Method::POST | Method::PUT | Method::PATCH | Method::DELETE', 'Method::POST | Method::PATCH | Method::DELETE'),
    ('a transcribed clip gets a JSON limit', '| "/transcribe"', '| "/other-transcribe"'),
    ('a picture gets a JSON limit', '"/media" | "/tools/media"', '"/other-media" | "/tools/media"'),
    ('a tool upload gets a JSON limit', '"/media" | "/tools/media"', '"/media" | "/other-tool-media"'),
    ('a file upload gets a JSON limit', '| "/upload"', '| "/other-upload"'),
    ('the declared length is checked too late', 'if length > limit as u64 {', 'if false && length > limit as u64 {'),
    ('a request at the limit is refused', 'if length > limit as u64 {', 'if length >= limit as u64 {'),
    ('the oversized connection stays reusable', 'header::CONNECTION, "close"', 'header::CONNECTION, "keep-alive"'),
    ('the refusal loses its error field', 'Json(json!({"error":message}))', 'Json(json!({"message":message}))'),
    ('a missing length is accepted', 'let Some(value) = request.headers().get(header::CONTENT_LENGTH) else {\n        return json_body_refused(StatusCode::BAD_REQUEST, "bad json");', 'let Some(value) = request.headers().get(header::CONTENT_LENGTH) else {\n        return next.run(request).await;'),
    ('the downstream extractor reintroduces its default', 'DefaultBodyLimit::max(limit)', 'DefaultBodyLimit::max(LARGE_JSON_BODY)'),
    ('janitor inspection refuses larger controls', 'to_bytes(body, JSON_BODY_LIMIT)', 'to_bytes(body, LARGE_JSON_BODY)'),
    ('a body of exactly two MiB is logged', 'if bytes.len() > LARGE_JSON_BODY', 'if bytes.len() >= LARGE_JSON_BODY'),
    ('the first byte above the log threshold is missed', 'if bytes.len() > LARGE_JSON_BODY', 'if bytes.len() > LARGE_JSON_BODY + 1'),
    ('the log tells the limit rather than bytes read', 'bytes={}", bytes.len()', 'bytes={}", limit'),
    ('the log contains the query', 'route={path} bytes={}", bytes.len()', 'route={} bytes={}", parts.uri, bytes.len()'),
    ('the log includes the body', 'route={path} bytes={}", bytes.len()', 'route={path} bytes={} {}", bytes.len(), String::from_utf8_lossy(&bytes[..128])'),
]
PLANTS = [(EDGE, 'host', what, old, new) for what, old, new in CHANGES]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
