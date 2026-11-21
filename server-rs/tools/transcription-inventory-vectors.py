#!/usr/bin/env python3
"""Record the Linux catalog, installed records and removals using disposable files."""
import importlib.util
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/transcription-inventory.json"


def record(tmp):
    spec = importlib.util.spec_from_file_location("host", ROOT / "tests/replay/host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    host.isolate(tmp)
    host.loopback_only()
    os.environ.update(CLARP_PLATFORM_OVERRIDE="linux",
                      CLARP_TRANSCRIPTION_REGISTRY=str(tmp / "inventory.json"),
                      CLARP_TRANSCRIPTION_MODELS=str(tmp / "models"))
    sys.path.insert(0, str(ROOT / "server"))
    from lib import transcription_models as models
    from lib.stt import installed_transcription_models

    cases = []
    files = ("config.json", "model.bin", "tokenizer.json")
    for pieces in itertools.product((False, True), repeat=len(files)):
        path = tmp / "model"
        path.mkdir(exist_ok=True)
        for name in files:
            (path / name).touch()
        registered = models.register("faster-whisper:small", str(path))
        for name, present in zip(files, pieces):
            if not present:
                (path / name).unlink()
        registered["_local_path"] = "/fixture/model"
        cases.append({"record": registered, "files": [name for name in files if (path / name).is_file()],
                      "models": installed_transcription_models(), "catalog": models.catalog_status()})
    from types import SimpleNamespace
    from unittest.mock import patch
    removals = []
    for kind in ("managed", "external", "outside", "unknown", "incomplete",
                 "incomplete-managed", "absent-managed", "active"):
        base = tmp / kind
        managed = base / "models"
        model_id = "faster-whisper:small"
        managed_case = kind in ("managed", "incomplete-managed", "absent-managed")
        target = managed / "faster-whisper--small" if managed_case else base / "external"
        target.mkdir(parents=True)
        for file in files:
            (target / file).touch()
        with patch.object(models, "MANAGED_MODELS", managed), patch.object(models, "REGISTRY", base / "registry.json"):
            if kind != "unknown":
                stored = str(target) if managed_case or kind == "outside" else None
                models.register(model_id, str(target), managed_root=stored)
            if kind in ("incomplete", "incomplete-managed"):
                (target / files[2]).unlink()
            if kind == "absent-managed":
                for file in files:
                    (target / file).unlink()
                target.rmdir()
            cfg = SimpleNamespace(whisper_model="small", whisper_enabled=kind == "active")
            with patch("lib.config.load", return_value=cfg), patch("lib.log.log") as logged:
                try:
                    models.remove(model_id)
                    error = None
                except ValueError as exc:
                    error = str(exc).replace(str(target), "<target>")
            removal_logs = [list(call.args[:2]) for call in logged.call_args_list
                            if call.args and call.args[0] in
                            ("transcriptionModelStoredPathMismatch", "transcriptionModelFolderRefused")]
            removals.append({"kind": kind, "error": error, "files_survive": target.exists(),
                             "logs": removal_logs,
                             "registered": any(row["id"] == model_id for row in models._read_registry())})
    return {"catalog": cases, "removal": removals}


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        print(json.dumps(record(Path(sys.argv[-1])), ensure_ascii=True, separators=(",", ":")))
    else:
        with tool_scratch.directory() as tmp:
            data = subprocess.check_output([sys.executable, __file__, "--isolated", tmp])
        OUT.write_bytes(data)
