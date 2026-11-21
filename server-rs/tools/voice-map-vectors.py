#!/usr/bin/env python3
"""Vectors from the reference's V2a maps and reviewed migration."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/voice-map-vectors.json'


@scratch.case('clarp-voice-map-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, voice, voice_migration, voice_profile

    uuid = "f0000000-0000-4000-8000-000000000000"
    canonical = [{"input": raw, "output": voice.canonical(raw)} for raw in (
        "", "{}", "{}", "  unknown  ", uuid, "en-US-Chirp3-HD-Kore", "0123456789abcdefghij",
        '{"gemini":"Kore","cartesia":"x","google":null,"empty":""}', '{"x":1}', "{invalid",
    )]
    cart, eleven = "cartesia", "elevenlabs"
    current = '{"cartesia":"new","elevenlabs":"later","gemini":"Kore"}'
    base = '{"cartesia":"old","elevenlabs":"original"}'
    merges = [{"stored": a, "incoming": b, "base": c, "output": voice.merge_provider_maps(a,b,c)}
              for a,b,c in (
                  (current, '{"cartesia":"old","elevenlabs":"edited"}', base),
                  (current, '{"cartesia":"old"}', base),
                  (current, '{"cartesia":null,"gemini":""}', None),
                  (current, '{}', None), (current, '{}', ''),
                  (uuid, '{"gemini":"Kore"}', None),
                  ("", uuid, None), (current, "en-US-Chirp3-HD-Kore", None),
              )]
    agents = [{"agent_id":"a1","persona":"One","session":"first","voice_id":"e-example"},
              {"agent_id":"a2","persona":"Two","session":"second","voice_id":uuid},
              {"agent_id":"a3","persona":"Two","session":"third","voice_id":"{}"}]
    contacts = [{"persona_id":"p1","name":"Contact","voice_id":"", "builtin":False}]
    host = voice_profile.HostView.build(config.Config(tts_provider=cart, tts_fallback=eleven), stored={})
    host.keys = {cart:True, eleven:True}
    host.agent_engines = {"one":"elevenlabs:scribe_v2","a3":"deepgram:nova-3"}
    host.agent_languages = {"two":"no,en","second":"en"}
    alone = voice_migration._per_provider_hosts(host)
    rows = [voice_migration._row(host, alone, "agent", {"agent_id":a["agent_id"],
              "name":a["persona"], "session":a["session"]}, a) for a in agents]
    rows += [voice_migration._row(host, alone, "contact", {"persona_id":a["persona_id"],
              "name":a["name"], "builtin":a["builtin"]}, {"persona":a["name"],"voice_id":a["voice_id"]})
              for a in contacts]
    report = {"voices":[row for row in rows if row],
              "transcription":voice_migration._transcription_plan(host, agents),
              "provider":cart,"fallback":eleven}
    report["digest"] = voice_migration.digest(report)
    return {"canonical":canonical,"merges":merges,"agents":agents,"contacts":contacts,"report":report}


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(),ensure_ascii=True))
        return
    ran=subprocess.run([sys.executable,'-I',__file__,'--child'],capture_output=True,text=True,
                       env=scratch.child_env({'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}),check=False)
    if ran.returncode:
        print(ran.stderr[-3000:],file=sys.stderr)
        raise SystemExit(ran.returncode)
    data=json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(data,sort_keys=True,ensure_ascii=True,indent=2)+'\n',encoding='ascii')
    print(f"voice-map vectors: {len(data['canonical'])} canonical, {len(data['merges'])} merge cases")

if __name__ == '__main__':
    main()
