#!/usr/bin/env python3
"""Planted changes to JSON request admission, draining, exceptions and logging."""
import runner

KERNEL = 'clarp-kernel/src/request_body.rs'
EDGE = 'clarp-host/src/edge.rs'
LIBRARY = 'clarp-host/src/library.rs'
RUNS = {
    'vectors': ['test', '--test', 'body_limits_vectors'],
    'host': ['test', '--test', 'host', '--', 'the_host_accepts_full_voice_batches_and_large_artifact_bodies'],
}
RUNS['framing'] = ['test', '--test', 'host', '--', 'body_framing_seen_at_the_local_door']
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, name, old, new) for name, old, new in changes)


rules(KERNEL, 'vectors', [
    ('the common limit loses the last byte', '16 * 1024 * 1024;', '16 * 1024 * 1024 - 1;'),
    ('the common limit admits another byte', '16 * 1024 * 1024;', '16 * 1024 * 1024 + 1;'),
    ('pairing loses its last byte', '64 * 1024;', '64 * 1024 - 1;'),
    ('pairing admits one extra byte', '64 * 1024;', '64 * 1024 + 1;'),
    ('a pairing exchange gets the common cap', 'Some(PAIRING_LIMIT)', 'Some(JSON_LIMIT)'),
    ('a DELETE bypasses the cap', '"POST" | "PUT" | "PATCH" | "DELETE"', '"POST" | "PUT" | "PATCH"'),
    ('a PATCH bypasses the cap', '"POST" | "PUT" | "PATCH" | "DELETE"', '"POST" | "PUT" | "DELETE"'),
    ('a PUT bypasses the cap', '"POST" | "PUT" | "PATCH" | "DELETE"', '"POST" | "PATCH" | "DELETE"'),
    ('a GET is treated as a submission', '"POST" | "PUT" | "PATCH" | "DELETE"', '"GET" | "POST" | "PUT" | "PATCH" | "DELETE"'),
    ('a transcribed clip gets a JSON limit', '| "/transcribe"', '| "/elsewhere"'),
    ('a picture gets a JSON limit', '"/media" | "/tools/media"', '"/elsewhere" | "/tools/media"'),
    ('a file upload gets a JSON limit', '| "/upload"', '| "/elsewhere"'),
    ('a body at the limit is refused', 'length > limit as u64', 'length >= limit as u64'),
    ('the first accepted large body is missed', 'length > LARGE', 'length > LARGE + 1'),
    ('two MiB exactly is logged', 'length > LARGE', 'length >= LARGE'),
    ('the maximum drain is closed instead', 'length > DRAINED_MOST', 'length >= DRAINED_MOST'),
    ('one byte beyond the drain is read', 'length > DRAINED_MOST', 'length > DRAINED_MOST + 1'),
    ('the refusal sentence differs', '"request body too large"', '"body too large"'),
    ('a family includes the dynamic suffix', ".split('/').next()", ".split('/').next_back()"),
])
rules(KERNEL,'host',[
    ('agent import gets the common cap', 'Some(IMPORT_LIMIT)', 'Some(JSON_LIMIT)'),
    ('media published by a tool gets the common cap', '"/media" | "/tools/media"', '"/media" | "/elsewhere"'),
])
rules(EDGE,'host',[
    ('the common extractor limit is lost', 'DefaultBodyLimit::max(limit)', 'DefaultBodyLimit::max(2 * 1024 * 1024)'),
    ('the janitor guard restores its old cap', 'to_bytes(body, request_body::JSON_LIMIT)', 'to_bytes(body, 2 * 1024 * 1024)'),
    ('a large refusal keeps the connection', 'if close {\n                return too_large(true);', 'if close {\n                return too_large(false);'),
    ('the refusal loses its error field','Json(json!({"error":message}))','Json(json!({"message":message}))'),
    ('accepted large bodies disappear from the journal','requestBodyLarge family=', 'requestBodySmall family='),
    ('refused large bodies disappear from the journal','requestBodyTooLarge family=', 'bodyDenied family='),
    ('a small refusal always closes','return too_large(!complete);', 'return too_large(complete);'),
])
rules(LIBRARY,'host',[
    ('text validation restores the old cap','to_bytes(body, clarp_kernel::request_body::JSON_LIMIT)', 'to_bytes(body, 2 * 1024 * 1024)'),
])

rules('clarp-host/src/connector.rs','host',[
    ('the relay forwards a refused body to authentication', '&& body.len() > limit', '&& body.len() < limit'),
])
rules(EDGE,'framing',[
    ('transfer coding reaches dispatch', 'if request.headers().contains_key(header::TRANSFER_ENCODING)', 'if false && request.headers().contains_key(header::TRANSFER_ENCODING)'),
    ('the transfer-coded refusal keeps the socket', 'if request.headers().contains_key(header::TRANSFER_ENCODING) {\n        return body_refusal(StatusCode::BAD_REQUEST, "bad json", true);', 'if request.headers().contains_key(header::TRANSFER_ENCODING) {\n        return body_refusal(StatusCode::BAD_REQUEST, "bad json", false);'),
    ('absence of length prevents route validation', 'let Some(value) = request.headers().get(header::CONTENT_LENGTH) else {','let Some(value) = request.headers().get(header::CONTENT_LENGTH) else {\n        if request.uri().path()=="/voice-events" {return body_refusal(StatusCode::BAD_REQUEST,"bad json",true);}'),
])
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
