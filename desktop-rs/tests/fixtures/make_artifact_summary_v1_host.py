"""Sanitize a raw capture (artifact_summary_v1_capture.py) into artifact_summary_v1_host.json.

usage: python3 make_artifact_summary_v1_host.py RAW.json HOST_SHA
The two big forms' and the lone-surrogate form's contents become a size note
(their markers keep the real bytes and hashes); the surrogate item stays as its
raw wire text. /server-info keeps only what the desktop reads.
"""
import hashlib
import json
import pathlib
import sys

raw, sha = json.load(open(sys.argv[1])), sys.argv[2]
ids = raw["ids"]
HIDDEN = {ids["big"], ids["big116"], ids["surrogate"]}


def c1(text):
    return "c1:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_item(item):
    item = dict(item)
    if isinstance(item.get("payload"), dict):
        item["payload"] = dict(item["payload"])
    for holder in (item, item.get("payload") or {}):
        if item["artifact_id"] in HIDDEN and isinstance(holder.get("content"), str):
            size = len(holder["content"].encode("utf-8", "surrogatepass"))
            holder["content"] = f"<omitted by the fixture: {size} bytes>"
    return item


def clean(capture):
    body = capture["body"]
    if isinstance(body, dict) and "artifacts" in body:
        body = {**body, "artifacts": [clean_item(a) for a in body["artifacts"]]}
    elif isinstance(body, dict) and "artifact" in body:
        body = {**body, "artifact": clean_item(body["artifact"])}
    return {"path": capture["path"], "status": capture["status"], "bytes": capture["bytes"], "body": body}


info = raw["server_info"]["body"]
fixture = {
    "_about": f"Real Host responses (feat/artifact-summary-v1 @ {sha}, in-process HTTP harness, temp DB, frozen clock). "
              "Big and lone-surrogate form contents are replaced by a size note; their markers keep the real bytes and hashes. "
              "Regenerate with artifact_summary_v1_capture.py + make_artifact_summary_v1_host.py; do not hand-edit.",
    "host_sha": sha,
    "ids": ids,
    "server_info": {"status": raw["server_info"]["status"], "body": {
        "server_id": info.get("server_id"),
        "capabilities": {"features": info["capabilities"]["features"]},
        "contract": {"host": info["contract"]["host"], "min_ios": info["contract"].get("min_ios"),
                     "features": {"artifact_summary_v1": info["contract"]["features"]["artifact_summary_v1"]}}}},
    "big116_content_c1": c1(json.loads(raw["items"]["big116"]["flat"]["raw"])["artifact"]["content"]),
    "surrogate_item_flat_raw": raw["items"]["surrogate"]["flat"]["raw"],
}
for key in ("summary", "summary_pending", "flat", "pinned_summary", "pinned_flat", "refused", "pinned_refused",
            "inbox_refused", "missing"):
    fixture[key] = clean(raw[key])
fixture["items"] = {name: {shape: clean(c) for shape, c in shapes.items()} for name, shapes in raw["items"].items()}
out = pathlib.Path(__file__).with_name("artifact_summary_v1_host.json")
out.write_text(json.dumps(fixture, ensure_ascii=False, indent=1) + "\n")
print(out, out.stat().st_size)
