"""Project actual Clarp turn/steering identity without merging unrelated jobs."""
from __future__ import annotations


def project(rows):
    traces = {row.get("trace_id"): row for row in rows if row.get("trace_id")}
    groups = {}
    for row in rows:
        trace = row.get("completion_trace_id") or row.get("trace_id") or row["delegation_id"]
        seen = set()
        while trace in traces and trace not in seen:
            seen.add(trace)
            parent = traces[trace].get("completion_trace_id")
            if not parent: break
            trace = parent
        groups.setdefault((row["session"], trace), []).append(row)
    output = []
    for members in groups.values():
        members.sort(key=lambda row: (row.get("created_at", 0), row["delegation_id"]))
        first = members[0]
        active = [row for row in members if row["status"] in ("accepted", "queued")]
        latest = max(active or members, key=lambda row: row.get("updated_at", row.get("created_at", 0)))
        requests = [str(row.get("request_text") or "").split("\n\n<oracle-reference-data>", 1)[0]
                    .split("\n\n<oracle-reporting-guidance>", 1)[0][:16000] for row in members]
        output.append({"id": first["delegation_id"], "session": first["session"], "status": latest["status"],
            "request": requests[0], "requests": requests,
            "operation_ids": [row["delegation_id"] for row in members],
            "result": "" if active else str(latest.get("result_text") or latest.get("error") or "")[:16000],
            "updated_at": max(row.get("updated_at", row.get("created_at", 0)) for row in members)})
    return sorted(output, key=lambda item: (item["updated_at"], item["id"]), reverse=True)
