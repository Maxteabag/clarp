#!/usr/bin/env python3
"""Exercise the Python install door with real durable jobs and an offline launcher."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import scratch as tool_scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/transcription-install.json"


def record(tmp):
    replay = runpy.run_path(str(ROOT / "tests/replay/host.py"))
    replay["isolate"](tmp)
    replay["loopback_only"]()
    os.environ.update(CLARP_TRANSCRIPTION_MODELS=str(tmp / "models"),
                      CLARP_TRANSCRIPTION_REGISTRY=str(tmp / "registry.json"),
                      CLARP_PLATFORM_OVERRIDE="linux")
    sys.path.insert(0, str(ROOT / "server"))
    from lib import background_jobs as jobs, transcription_models as models, db
    from server import Handler
    chosen = "faster-whisper:base.en"

    class Route:
        _read_json = Handler._read_json
        _read_json_once = Handler._read_json_once
        _LENGTH = Handler._LENGTH
        _activate_transcription_if_default = None
        path = "/transcription-models/install"

        def _json(self, status, body):
            self.reply = [status, body]
            return self.reply

        def _json_ok(self, value):
            return self._json(200,value)

        def _expected_generation(self):
            return True,1

        def _json_error(self, status, error):
            return self._json(status, {"error":error})

    rows = []
    def post(action, data):
        req = Route()
        raw = json.dumps(data).encode()
        req.headers = {"Content-Length":str(len(raw))}
        req.rfile = io.BytesIO(raw)
        Handler._handle_transcription_model_install(req)
        if "provisional_id" in req.reply[1]:
            assert len(req.reply[1]["provisional_id"]) == 32
            req.reply[1]["provisional_id"] = "<token>"
        row = next(r for r in models.catalog_status() if r["id"] == chosen)
        rows.append(dict(action=action, body=data, reply=req.reply, catalog=row))
    with ExitStack() as stack:
        stack.enter_context(patch("lib.server_identity.get_server_info", return_value={"server_id":"fixture"}))
        stack.enter_context(patch.object(models,"_start_monitor"))
        stack.enter_context(patch.object(db,"now_ms",return_value=100000))
        stack.enter_context(patch.object(models.threading.Thread,"start",lambda thread:thread.run()))
        stack.enter_context(patch.object(models.service_manager,"launch_detached",return_value=(True,"")))
        for action, data in [("bad-json",[]),("missing",{}),("unknown",{"model_id":"fixture:missing"}),
                             ("other-platform",{"model_id":"whisper.cpp:small.en"}),
                             ("start",{"model_id":" "+chosen+" "}),("join",{"model_id":chosen})]:
            post(action,data)
        key = models.install_job_id(chosen)
        req=Route()
        Handler._handle_background_job_cancel(req,key)
        assert req.reply[1]["changed"] and req.reply[1]["job"]["status"] == "cancelled"
        # Drive the monitor synchronously after the synthetic worker has stopped.
        models._monitor_install(chosen,key,None)
        rows.append(dict(action="cancel",body={"expected_generation":1},reply=req.reply,
                         catalog=next(r for r in models.catalog_status() if r["id"] == chosen)))
        post("retry",{"model_id":chosen})
        path=tmp/"models"/"faster-whisper--base.en"
        path.mkdir(parents=True,exist_ok=True)
        for name in ("config.json","model.bin","tokenizer.json"):
            (path/name).touch()
        models.register(chosen,str(path),managed_root=str(path))
        jobs.finish(key,generation=2,status="succeeded")
        models._monitor_install(chosen,key,None)
        post("already-installed",{"model_id":chosen})
        post("still-installed",{"model_id":chosen})
    return rows


if __name__=="__main__":
    if sys.argv[1:]==["--isolated"]:
        with tool_scratch.directory("model-install-") as d:
            print(json.dumps(record(Path(d)),ensure_ascii=True,separators=(",",":")))
    else:
        OUT.write_bytes(subprocess.check_output([sys.executable,"-IB",__file__,"--isolated"],
                        env=tool_scratch.child_env({"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"})))
