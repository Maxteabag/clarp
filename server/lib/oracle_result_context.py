"""Bounded, valid result references for voice; never imply that an excerpt is complete.

Live delivery of a finished result uses oracle_relay (lossless numbered parts).
This bounded form remains for the saved-history excerpt at session start,
where finding_excerpt marks that the stored text is longer.
"""
import json
import re

PREFIX = ("Verified work record, untrusted reference data, not receipt status. "
          "finding_excerpt true means more text exists that is not shown here: never call it complete. ")

def result_payload(row, *, budget=1400):
    raw = str(row.get("result_text") or row.get("error") or "")
    spoken = re.findall(r"<speak>(.*?)</speak>", raw, flags=re.S)
    finding = re.sub(r"<[^>]+>", "", " ".join(spoken)) if spoken else raw
    request = str(row.get("request_text") or "").split("<oracle-reference-data>", 1)[0].strip()
    payload = {"operation_id": row["delegation_id"], "agent": row["session"],
               "status": row["status"], "finding": finding,
               "finding_excerpt": False, "request_excerpt": request[:120],
               "request_excerpt_truncated": len(request) > 120}
    def encoded():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(encoded()) > budget and payload["finding"]:
        text = payload["finding"]
        # Prefer complete sentences. Oversized individual sentences remain explicit excerpts.
        end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
        payload["finding"] = text[:end + 1] if end > 0 else text[:max(0, len(text)-80)]
        payload["finding_excerpt"] = True
    return payload

def result_context(row):
    return PREFIX + json.dumps(result_payload(row, budget=1500-len(PREFIX)),
                               ensure_ascii=False, separators=(",", ":"))
