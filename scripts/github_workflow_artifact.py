#!/usr/bin/env python3
"""Create and update a GitHub Actions workflow-run artifact."""
from __future__ import annotations
import argparse, json, pathlib, subprocess, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from agent_artifacts import _request  # type: ignore
from lib import service_manager  # type: ignore

def snapshot(repo: str, run_id: str) -> dict:
    out = subprocess.run(["gh","run","view",run_id,"--repo",repo,"--json","databaseId,url,name,status,conclusion,jobs,headBranch,headSha"], text=True, capture_output=True, timeout=30, check=True)
    raw=json.loads(out.stdout); steps=[s for j in raw.get("jobs") or [] for s in j.get("steps") or []]
    return {"provider":"github","run_id":str(raw.get("databaseId") or run_id),"run_url":str(raw.get("url") or ""),"workflow_name":str(raw.get("name") or "GitHub Actions"),"repository":repo,"branch":str(raw.get("headBranch") or ""),"commit":str(raw.get("headSha") or ""),"current_step":next((str(s.get("name") or "") for s in steps if s.get("status")=="in_progress"),""),"total_steps":len(steps),"completed_steps":sum(s.get("status")=="completed" for s in steps),"conclusion":str(raw.get("conclusion") or ""),"github_status":str(raw.get("status") or "")}
def state(p: dict) -> str:
    if p.get("github_status")!="completed": return "active"
    return "completed" if p.get("conclusion") in {"success","neutral","skipped"} else "failed"
def create(session: str, repo: str, run_id: str) -> str:
    p=snapshot(repo,run_id); a=_request("POST","/artifacts",{"session":session,"type":"workflow_run","title":p["workflow_name"],"summary":f"GitHub Actions · {repo}","status":state(p),"payload":p})["artifact"]; aid=a["artifact_id"]
    if state(p) in {"completed","failed"}: return aid
    command = [sys.executable, str(pathlib.Path(__file__).resolve()),
               "watch", aid, repo, run_id]
    ok, error = service_manager.launch_detached(
        command, unit=f"clarp-gh-{aid}")
    if not ok:
        _request("POST",f"/artifacts/{aid}",{"status":"failed","payload_patch":{"conclusion":"watcher_launch_failed"}})
        raise RuntimeError(error or "could not start workflow watcher")
    return aid
def watch(aid: str, repo: str, run_id: str) -> None:
    failures=0
    while True:
        try:
            p=snapshot(repo,run_id); s=state(p); _request("POST",f"/artifacts/{aid}",{"status":s,"payload_patch":p}); failures=0
            if s in {"completed","failed"}: return
            time.sleep(10)
        except (OSError,subprocess.SubprocessError,json.JSONDecodeError):
            failures+=1
            if failures>=12:
                try: _request("POST",f"/artifacts/{aid}",{"status":"failed","payload_patch":{"conclusion":"watcher_failed"}})
                except OSError: pass
                return
            time.sleep(min(60,5*(2**min(failures,4))))
def main() -> int:
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True); a=sub.add_parser("start"); a.add_argument("session"); a.add_argument("repo"); a.add_argument("run_id"); w=sub.add_parser("watch"); w.add_argument("artifact_id"); w.add_argument("repo"); w.add_argument("run_id"); x=ap.parse_args()
    print(create(x.session,x.repo,x.run_id)) if x.cmd=="start" else watch(x.artifact_id,x.repo,x.run_id); return 0
if __name__=="__main__": raise SystemExit(main())
