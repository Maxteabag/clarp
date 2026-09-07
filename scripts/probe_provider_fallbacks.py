#!/usr/bin/env python3
"""Paid, explicitly invoked proof that a fallback answers on every provider.

Runs the real ``model_fallbacks.json_call`` against each installed routing
backend in a throwaway workspace and records whether it returned a
schema-valid assistant answer. A provider that is out of quota is reported as
such rather than as a broken code path: that is the failure this feature
exists to survive, and its category is asserted, not guessed.

    scripts/probe_provider_fallbacks.py --output evidence.json
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "maxLength": 240}},
    "required": ["answer"],
    "additionalProperties": False,
}
PROMPT = (
    "Explain, in one short present-tense sentence in everyday language, what the "
    "shell command `ls -la` shows someone. Answer with the sentence only."
)
# Antigravity encodes effort in the model id; the rest use their own default.
MODELS = {"agy": "gemini-3.8-flash-low"}
# Grok Build's account balance is exhausted (HTTP 402 Payment Required), so a
# live probe there proves nothing about this code. Pass --backend grok to
# include it once the account has balance again.
UNFUNDED = {"grok"}


def probe(backend, model, timeout):
    from lib import model_fallbacks

    started = time.monotonic()
    try:
        answer = model_fallbacks.json_call(
            {"backend": backend, "model": model, "effort": ""},
            PROMPT,
            SCHEMA,
            timeout=timeout,
        )
        text = answer["answer"]
        return {
            "backend": backend,
            "model": model,
            "answered": True,
            "usable": bool(text.strip())
            and any(w in text.lower() for w in ("file", "directory", "folder", "list")),
            "answer": text,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except Exception as error:
        return {
            "backend": backend,
            "model": model,
            "answered": False,
            "provider_failure": model_fallbacks.is_provider_failure(error),
            "category": getattr(error, "category", ""),
            "error": str(error)[:300],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", action="append", dest="backends")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="clarp-provider-probe-") as folder:
        root = Path(folder)
        for key, path in {
            "CLARP_SHARE_DIR": "share",
            "CLARP_CONFIG_DIR": "config",
            "CLARP_CACHE_DIR": "cache",
            "CLAUDE_PWA_DB": "state.sqlite",
        }.items():
            os.environ[key] = str(root / path)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from lib import backends as registry, db

        wanted = args.backends or [a.id for a in registry.routing_adapters()]
        results = []
        for backend in wanted:
            adapter = registry.get(backend)
            if adapter is None or not adapter.supports_routing:
                results.append({"backend": backend, "skipped": "not a routing backend"})
                continue
            if shutil.which(adapter.required_binary) is None:
                results.append({"backend": backend, "skipped": "CLI not installed"})
                continue
            if backend in UNFUNDED and not args.backends:
                results.append({"backend": backend, "skipped": "account unfunded"})
                continue
            results.append(probe(backend, MODELS.get(backend, ""), args.timeout))
            print(json.dumps(results[-1]), flush=True)

        evidence = {
            "answered": sorted(r["backend"] for r in results if r.get("usable")),
            "out_of_quota": sorted(
                r["backend"] for r in results if r.get("category") == "usage_limit"
            ),
            "unreachable": sorted(
                r["backend"]
                for r in results
                if not r.get("answered")
                and not r.get("skipped")
                and not r.get("provider_failure")
            ),
            "results": results,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        db.close_local()
        print(json.dumps({k: evidence[k] for k in list(evidence)[:3]}, indent=2))
        # Every reachable provider must either answer or name a provider failure.
        raise SystemExit(1 if evidence["unreachable"] else 0)


if __name__ == "__main__":
    main()
