#!/usr/bin/env python3
"""Capture directory suggestions and launch frequencies using only an isolated reference store."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT/"server-rs/crates/clarp-kernel/tests/fixtures/launch-directories.json"


def capture(place):
    from lib import agents, db, launch_directories
    from lib.routes.settings import SettingsRoutes
    # Rank names in one fixed namespace. A random temporary prefix can itself
    # match fuzzy queries, even when replaced in the serialized answer.
    home = Path("/fixture/home")
    def actual(path):
        return place/path.relative_to("/fixture")
    is_dir = Path.is_dir
    iterdir = Path.iterdir
    def children(path):
        return [path/p.name for p in iterdir(actual(path))]
    def expand(path):
        parts=path.parts
        return home.joinpath(*parts[1:]) if parts and parts[0]=="~" else path
    names = ["pr", "project", "Project TWO", ".private", "Stra" + chr(223) + "e", "shared", "janitor", "orphan"]
    names += [f"p{i:02d}" for i in range(40)]
    for name in names:
        actual(home/name).mkdir(exist_ok=True)
    actual(home/"project"/"inner").mkdir()
    (place/"homesibling").mkdir()
    actual(home/"link").symlink_to(actual(home/"project"), target_is_directory=True)
    paths = [str(home/name) for name in names]
    launches = []
    for index, name in enumerate(["old-missing", *names, "project", "Project TWO", "janitor", "shared"]):
        ts = 1000 + index // 3
        path = str(home/name)
        with patch.object(agents,"now_ms",return_value=ts):
            agents.record_path_usage(" "+path+" ")
        launches.append([path,ts])
    owners = {}
    for session,name,janitor,deleted in [("one","janitor",1,1),("two","shared",1,0),("three","shared",0,1)]:
        path = str(home/name)
        aid=agents.create_agent(persona=session,voice_id="",cwd=path,session=session)
        db.conn().execute("UPDATE agents SET is_janitor=?,deleted_at=? WHERE agent_id=?", (janitor,123 if deleted else None,aid))
        owners[session]=[path,bool(janitor)]

    favored=[]
    for limit in ["", "bad","0","-2","1","2","5","19","20","21","1_0",str(10**50),"  +2 ",chr(0x0662),chr(0x0967)+"_"+chr(0x0966),"9"*4301]:
        dest=[]
        handler=SimpleNamespace(_query=lambda:{"limit":[limit]},_json_ok=dest.append)
        SettingsRoutes._handle_favorite_paths(handler)
        favored.append({"limit":limit,"answer":dest[0]})

    cases=[]
    def one(q,rc=0,rows=None,backup=None,fail=None):
        rows=paths[::-1] if rows is None else rows
        backup=[] if backup is None else backup
        calls=[]
        def run(args,**kwargs):
            calls.append(args[1:])
            if fail==len(calls):
                raise subprocess.TimeoutExpired(args,.5)
            return SimpleNamespace(returncode=rc if len(calls)==1 else 9,
                                   stdout="\n".join(rows if len(calls)==1 else backup))
        with patch.object(launch_directories.subprocess,"run",side_effect=run), patch.object(Path,"is_dir",lambda p:is_dir(actual(p))), patch.object(Path,"iterdir",children), patch.object(Path,"expanduser",expand):
            answer=launch_directories.lookup(q,home=home,zoxide="fake")
        cases.append(dict(q=q,rc=rc,rows=rows,backup=backup,fail=fail,calls=calls,answer=answer))
    for q in ["", "pr","PR","./pr","./project/","project/","~","~sibling",str(home/"project"),
              "strasse", " p t ", "p\tt",("x/"*256)[:511],"x/"*256,"x/"*256+"z", ".p","p00/", "link/"]:
        one(q,0,[paths[1],str(home/"link"),paths[0],paths[1],str(home/"old-missing")])
    for q in ["", "p", "P r","p\tt", "strasse"]:
        one(q,1,[],paths)
    one("p",0,[str(home/"missing")]*99+[paths[1],paths[0]])
    one("p",1,[],[str(home/"missing")]*998+[str(home/"project"),str(home/"pr"),str(home/"private")])
    one("p",1,[],[str(home/"project")]*100+[str(home/"pr")])
    one("pr",1,[],paths,1)
    one("pr",1,[],paths,2)
    for n in (15,16,17):
        one("unknown",0,paths[:n])
    # No query candidate is required to match zoxide's successful output.
    one("no-such-name",0,["~/project",str(home/"project"),str(home/"./pr"),str(home)+"/project//inner/"])
    existing=[str(home),"/fixture/homesibling",str(home/"project/inner"),str(home/"link"),str(home/"link/inner"),*paths]
    # Observe the ranked candidates before existence checks and the 16-result limit.
    # No recent rows, explicit directories or home candidate can obscure a cutoff.
    ranked_cases=[]
    for fallback in (False,True):
        for size in [0,1,99,100,101,999,1000,1001]:
            raw=[f"/synthetic/{i}" for i in range(size)]
            if fallback: raw=[p+("-Az" if i%11==9 or i>=998 else "") for i,p in enumerate(raw)]
            q="A Z"
            def answer(args,**kwargs):
                if fallback and "--" in args:return SimpleNamespace(returncode=1,stdout="")
                return SimpleNamespace(returncode=1 if fallback else 0,stdout="\n".join(raw))
            seen=[]
            def expanded(p):
                seen.append(str(p))
                return p
            with patch.object(launch_directories.db,"conn",return_value=SimpleNamespace(execute=lambda *args:[])), patch.object(Path,"expanduser",expanded), patch.object(Path,"is_dir",return_value=False), patch.object(Path,"iterdir",return_value=iter(())), patch.object(launch_directories.subprocess,"run",side_effect=answer):
                launch_directories.lookup(q,home=home,zoxide="fake")
            ranked_cases.append(dict(fallback=fallback,size=size,q=q,seen=seen))
    # Nonmatching subsequences can contain all the requested letters in the wrong order.
    q="ZA"
    ranked_cases.append(dict(fallback=True,size=0,q=q,raw=["/synthetic/az","/synthetic/z","/synthetic/zA"],seen=[]))
    raw=ranked_cases[-1]["raw"]
    def ordered(args,**kwargs):
        return SimpleNamespace(returncode=1,stdout="\n".join(raw) if "--" not in args else "")
    def observed(p):
        ranked_cases[-1]["seen"].append(str(p))
        return p
    with patch.object(launch_directories.db,"conn",return_value=SimpleNamespace(execute=lambda *args:[])), patch.object(Path,"expanduser",observed), patch.object(Path,"is_dir",return_value=False), patch.object(Path,"iterdir",return_value=iter(())), patch.object(launch_directories.subprocess,"run",side_effect=ordered):
        launch_directories.lookup(q,home=home,zoxide="fake")
    # All children are considered for their lexical/prefix ordering before truncation.
    # Returning no candidate as a directory on the second pass exposes the explicit list.
    completions=[]
    for count in (0,1,29,30,31,40):
        for prefix in ("p","P","strasse"):
            names=[f"p{i:02d}" for i in reversed(range(count))]+["Stra"+chr(223)+"e",".private"]
            seen=[]
            def expand(p):
                seen.append(p.name)
                return p
            with patch.object(launch_directories.db,"conn",return_value=SimpleNamespace(execute=lambda *args:[])), patch.object(Path,"expanduser",expand), patch.object(Path,"is_dir",side_effect=[False]+[True]*len(names)+[False]*len(names)), patch.object(Path,"iterdir",return_value=[home/n for n in names]), patch.object(launch_directories.subprocess,"run",return_value=SimpleNamespace(returncode=0,stdout="")):
                launch_directories.lookup(prefix,home=home,zoxide="fake")
            completions.append(dict(names=names,prefix=prefix,seen=seen))
    probes=[]
    def absent(path):
        probes.append(str(path))
        return False
    with patch.object(Path,"is_dir",absent), patch.object(launch_directories.subprocess,"run",return_value=SimpleNamespace(returncode=1,stdout="")):
        launch_directories.lookup("",home=home,zoxide="fake")
    assert probes[0]==str(home)
    spellings=[dict(value=v,expected=str(Path(v))) for v in ("", ".", "./.", "./abc/", "//", "///", "//server//path", "a/./../b", "../../../.", "/.", "~example/a/.", "a/ /b")]
    fixture=dict(spellings=spellings,ranked=ranked_cases,completions=completions,recent=probes[1:],home=str(home),existing=existing,launches=launches,owners=owners,favorites=favored,cases=cases)
    return fixture


if __name__=="__main__":
    spec=importlib.util.spec_from_file_location("replay",ROOT/"tests/replay/host.py")
    replay=importlib.util.module_from_spec(spec);spec.loader.exec_module(replay)
    with tempfile.TemporaryDirectory() as tmp:
        place=Path(tmp)
        (place/"scenario-config.toml").write_text("")
        replay.isolate(place)
        replay.loopback_only()
        sys.path.insert(0,str(ROOT/"server"))
        data=capture(place)
        from unicodedata import decimal
        zeroes=[n for n in range(sys.maxunicode+1) if decimal(chr(n),None)==0]
        (ROOT/"server-rs/crates/clarp-kernel/src/launch-decimal-zeroes.json").write_text(json.dumps(zeroes)+"\n")
        DEST.write_text(json.dumps(data,ensure_ascii=True,separators=(",",":"))+"\n",encoding="ascii")
