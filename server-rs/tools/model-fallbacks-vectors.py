#!/usr/bin/env python3
"""Model policy, retry decisions, receipts and structured replies of an isolated Python Host."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/model-fallbacks-vectors.json"


def capture(root):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (root / "scenario-config.toml").write_text("")
    replay.isolate(root)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / "server"))
    from lib import agents, janitors, janitor_design_policy as policy, model_fallbacks as retry
    sessions = {key: dict(session=key, model="m", backend="codex", revision=4, template_id=role,
                          execution={"executor": how, "provider": "local" if how=="deterministic" else "codex"})
                for key, role, how in [("router","message-delegator","ephemeral"), ("keeper", "goal-keeper", "ephemeral"),
                                       ("scheduled", "scheduled-prompt", "ephemeral"),
                                       ("audio", "audio-bookkeeper", "deterministic")]}
    agents.create_agent(persona="Example", voice_id="", cwd="/tmp", session="ordinary", backend="codex")
    ordinary=agents.get_by_session("ordinary")["agent_id"]
    primary={"provider":"codex","model":"primary"}
    alt={"provider":"claude","model":"alternate"}
    policy_cases=[]
    with patch.object(janitors,"get",side_effect=sessions.get), patch.object(policy.backends,"get",side_effect=lambda p:p in ("codex","claude")), patch.object(policy.backends,"is_valid_model",side_effect=lambda p,m:m!="invalid"), patch.object(policy.backends,"unknown_model_detail",return_value=""):
        patches=[
            (None,0),({"unknown":1},0),({"inherit_sessions":"keeper"},0),
            ({"inherit_sessions":["absent"]},0),({"inherit_sessions":[False]},0),
            ({"inherit_sessions":["keeper","keeper"]},0),({"inherit_sessions":["keeper"]},0),
            ({"model_chain":[]},0),({"model_chain":"wrong"},0),({"model_chain":[None]},0),
            ({"model_chain":[{**primary,"effort":"low"}]},0),
            ({"model_chain":[{"model":"m"}]},0),({"model_chain":[{"model":" ","provider":"codex"}]},0),
            ({"model_chain":[{"model":"x"*201,"provider":"codex"}]},0),
            ({"model_chain":[{"model":"invalid","provider":"codex"}]},0),
            ({"model_chain":[{"provider":"other","model":"m"}]},0),({"model_chain":[primary,primary]},0),
            ({},False),({},0.0),({},"0"),({},1),
            ({"model_chain":[primary,alt]},0),({"inherit_sessions":["keeper","scheduled"]},1),
            ({"inherit_sessions":["router"]},1),
            ({"inherit_sessions":["audio"]},2),({"model_chain":[alt,primary]},2),
            ({"inherit_sessions":[]},3),({"model_chain":[{"provider":"openai","model":"x"*200}]},4),
            ({},5),({"model_chain":[{"provider":"codex","model":str(i)} for i in range(30)]},6)]
        for patch_body,rev in patches:
            try:
                body=policy.configure(patch_body,rev)
                answer=[200,body]
            except janitors.JanitorError as e:
                answer=[e.status,{"error":str(e),"code":e.code}]
            except ValueError as e:
                answer=[400,{"error":str(e)}]
            policy_cases.append(dict(patch=patch_body,revision=rev,answer=answer,
                                     kept=policy.configuration(),effective={s:policy.effective_chain(s) for s in sessions}))

    failures=[]
    for text in ["usage limit reached","fetch failed","Error 529 overloaded","runner exited rc=1",
                 "an unexpected provider answer","aborted by user","turn_aborted by user", "permission denied",
                 "approval REQUIRED","NOT AUTHORIZED","ownership lost","configuration changed",
                 "fetch failed: permission denied",""]:
        failures.append({"text":text,"retry":retry._as_provider_failure(RuntimeError(text)) is not None})

    schema={"type":"object","properties":{"answer":{"type":"string","maxLength":5}},"required":["answer"],"additionalProperties":False}
    shapes=[]
    for before,answer,pad in [("",'{"answer":"yes"}',0),("noise {bad} ",'{"answer":"fine"}',0),
                              ("",'{"answer":false}',0),("",'{"answer":"abcdef"}',0),
                              ("",'{"answer":"done","toolAction":1}',0),("",'{"toolSummary":1}',0),
                              ("{ "*63,'{"answer":"ok"}',0),("{ "*64,'{"answer":"ok"}',0),
                              ("",'{"answer":"a"}',511986),("",'{"answer":"a"}',511987)]:
        try: expected=retry.schema_object(before+answer+" "*pad,schema)
        except retry.ProviderFailure: expected=None
        shapes.append(dict(before=before,answer=answer,pad=pad,expected=expected))
    permissive={"type":"object","required":["answer"]}
    for flag in ("toolAction","toolSummary"):
        text=json.dumps({"answer":"discard",flag:"finished"})+' {"answer":"kept"}'
        shapes.append(dict(before="",answer=text,pad=0,schema=permissive,expected=retry.schema_object(text,permissive)))
    calls=[]
    def cfg(name):return dict(backend="codex",model=name,effort="")
    for idx,(models,results,repeat) in enumerate([
        ([],["fail"],1),(["b"],["ok"],1),(["a","b","c"],["fail","ok"],1),
        (["b","c"],["fail","fail","ok"],1),(["b","c"],["fail","cancel"],1),
        (["b"],["deny"],1),(["b"],["fail","deny"],1),(["b"],["fail","ok"],2),
        (["b"],["fail","fail"],2)]):
        request=f"case-{idx}"
        for _ in range(repeat):
            tried=[]
            def invoke(model):
                result=results[len(tried)]
                tried.append(model["model"])
                if result=="fail":raise RuntimeError("provider unavailable")
                if result=="deny":raise RuntimeError("permission denied")
                if result=="cancel":raise retry.Cancelled("request is no longer current")
                return model["model"]
            with patch.object(retry,"get",return_value=dict(models=list(map(cfg,models)),revision=1)):
                try: result={"ok":retry.execute(ordinary,request,cfg("a"),invoke)}
                except RuntimeError as e: result={"error":str(e)}
            receipts=[{k:r[k] for k in ("attempt","backend","model","effort","reason","status","result_json")}
                      for r in retry.attempts(ordinary,request)]
            calls.append(dict(request=request,models=models,results=results,tried=tried,result=result,receipts=receipts))
    from lib import routing_input
    routing = []
    for backend in ("claude","codex","grok","muse","agy","opencode","deepseek","unknown"):
        for args in [[], ["@prompt"], ["-p","@prompt"], ["--","@prompt"], ["--print=@prompt"],
                     ["unrelated","@prompt","tail"], ["@prompt","-p","@prompt"]]:
            prompt="example"
            argv=[v.replace("@prompt",prompt) for v in args]
            with routing_input.prepared(backend,argv,prompt) as call:
                result=list(call.argv)
                where="stdin" if call.input is not None else "argv"
                if "--prompt-file" in result:
                    path=Path(result[-1])
                    assert path.read_text()==prompt
                    where="file"
                    result[-1]="@file"
                routing.append(dict(backend=backend,args=args,unit="e",count=0,prompt=prompt,
                                    argv=[v.replace(prompt,"@prompt") for v in result],where=where))
    for backend in ("agy","opencode","codex"):
        for unit,count in [("x",131070),("x",131071),("x",131072),(chr(0xe9),65535),(chr(0xe9),65536)]:
            prompt=unit*count
            for prefix in ("", "--print="):
                raw = prefix+prompt
                try:
                    with routing_input.prepared(backend,[raw],prompt) as call:
                        where="stdin" if call.input is not None else "argv"
                        projected=[v.replace(prompt,"@prompt") for v in call.argv]
                        result=dict(argv=projected,where=where)
                except routing_input.PromptTooLarge as e:
                    result={"error":str(e)}
                routing.append(dict(backend=backend,args=[prefix+"@prompt"],unit=unit,count=count,
                                    prompt="",**result))
    from lib import orchestrator
    import subprocess
    encodings=[]
    for backend in ("claude","codex","grok","muse","agy","opencode"):
        for points in ([0], [0x61,0xe9,0x1f642], [0xdc7f], [0xdc80], [0xdce9,0xdcff],
                       [0xdd00], [0xd800], [0xdbff], [0xdfff], [0xd7ff,0xe000,0x10ffff]):
            prompt="".join(map(chr,points))
            argv=[backend,*({"grok":["-p"],"muse":["--"]}.get(backend,[])),prompt]
            try:
                with routing_input.prepared(backend,argv,prompt) as call:
                    if call.input is not None: raw=call.input
                    elif "--prompt-file" in call.argv: raw=Path(call.argv[-1]).read_bytes()
                    else: raw=call.argv[-1].encode("utf-8","surrogateescape")
                    result={"bytes":list(raw)}
            except routing_input.PromptNotEncodable as e:
                result={"error":str(e)}
            encodings.append(dict(backend=backend,points=points,**result))
    timeouts=[]
    for backend,seconds in [("codex",30),("opencode",0.4),("agy",1.2345678),("grok",0.00001),("muse",1000000),
                            ("grok",0.000012345678),("muse",1234567.8)]:
        err=subprocess.TimeoutExpired([backend,"private-example"],seconds)
        failure=retry.content_free(err,backend)
        timeouts.append(dict(backend=backend,seconds=seconds,expected=str(failure),origin=failure.origin_type))
    historical=[]
    for line in ["ordinary error","", "Command '['/opt/bin/agy', '--print=private-example']' timed out after 30.0 seconds",
                 "Command '[]' timed out after 0.4 seconds","Command '['', 'private-example']' timed out after 0.001 seconds",
                 "Command '['codex', 'line1\nline2']' timed out after 7 seconds","prefix Command '[]' timed out after 2 seconds",
                 "Command '[]' timed out after 2 seconds trailing"]:
        historical.append(dict(stored=line,expected=orchestrator.public_error(line)))
    return dict(encodings=encodings,timeouts=timeouts,historical=historical,routing=routing,policy=policy_cases,failures=failures,schema=schema,shapes=shapes,calls=calls)


if __name__=="__main__":
    with tempfile.TemporaryDirectory() as root:
        data=capture(Path(root))
    OUT.write_text(json.dumps(data,sort_keys=True,indent=1)+"\n",encoding="ascii")
