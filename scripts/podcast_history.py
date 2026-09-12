#!/usr/bin/env python3
"""Read saved podcast conversations, source snapshots and explicit feedback."""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from agent_artifacts import _config, _request


def query(path, values):
    return path+"?"+urllib.parse.urlencode({k:v for k,v in values.items() if v is not None and v != ""})


def full_conversation(ident, request=_request):
    path="/podcast-history/"+urllib.parse.quote(ident, safe="")
    value=request("GET", path)
    through=value["through_event_id"]
    seen=set()
    while value.get("next_event_id") is not None:
        cursor=value["next_event_id"]
        if cursor in seen: raise ValueError("History cursor did not advance")
        seen.add(cursor)
        page=request("GET",query(path,{"after_event_id":cursor,"through_event_id":through}))
        value["events"].extend(page["events"])
        value["next_event_id"]=page["next_event_id"]
    value["complete_through_event_id"]=through
    return value


def download_images(value, directory):
    directory.mkdir(parents=True,exist_ok=True)
    base,token=_config()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): raise ValueError("Media redirect refused")
    opener=urllib.request.build_opener(NoRedirect())
    for item in value["images"]:
        path=item["url"]
        if not re.fullmatch(r"/media/asset_[A-Za-z0-9_]+",path): raise ValueError("Invalid saved image URL")
        if item.get("asset_deleted_at") is not None:
            item["download_error"]="Asset was deleted";continue
        req=urllib.request.Request(base+path,headers={"Authorization":"Bearer "+token} if token else {})
        with opener.open(req,timeout=30) as response: blob=response.read(4*1024*1024+1)
        if len(blob)>4*1024*1024 or hashlib.sha256(blob).hexdigest()!=item["sha256"]:
            raise ValueError("Saved image integrity mismatch")
        output=directory/(item["asset_id"]+".jpg")
        output.write_bytes(blob);item["local_path"]=str(output.resolve())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    search=sub.add_parser("search",help="Search questions, answers, context and explicit feedback")
    search.add_argument("query",nargs="?",default="")
    search.add_argument("--source",default="",help="Source or feedback-target artifact ID")
    search.add_argument("--episode",default="",help="Podcast audio artifact ID")
    search.add_argument("--session",default="")
    search.add_argument("--feedback-only",action="store_true")
    search.add_argument("--before",default="")
    search.add_argument("--limit",type=int,default=50)
    sources=sub.add_parser("sources",help="Find plan/form/document artifact IDs")
    sources.add_argument("query",nargs="?",default="")
    sources.add_argument("--offset",type=int,default=0)
    show=sub.add_parser("show",help="Retrieve every transcript page and the original source context")
    show.add_argument("conversation_id")
    show.add_argument("--images-dir",type=pathlib.Path)
    show.add_argument("--output",type=pathlib.Path)
    args=parser.parse_args(argv)
    try:
        if args.command=="search":
            value=_request("GET",query("/podcast-history",{"search":args.query,"source_artifact_id":args.source,
                "artifact_id":args.episode,"session":args.session,"feedback_only":int(args.feedback_only),
                "before":args.before,"limit":args.limit}))
        elif args.command=="sources":
            value=_request("GET",query("/podcast-history/sources",{"search":args.query,"offset":args.offset}))
        else:
            value=full_conversation(args.conversation_id)
            if args.images_dir: download_images(value,args.images_dir)
        encoded=json.dumps(value,ensure_ascii=False,indent=2)+"\n"
        if args.command=="show" and args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded)
            print(json.dumps({"output":str(args.output.resolve()),"conversation_id":args.conversation_id,
                              "events":len(value["events"]),"complete_through_event_id":value["through_event_id"]}))
        else: print(encoded,end="")
    except urllib.error.HTTPError as exc:
        print("Podcast history request failed (HTTP "+str(exc.code)+"). Check Host version and full-device authentication.",file=sys.stderr);return 1
    except (ValueError,OSError,KeyError) as exc:
        print("Podcast history: "+str(exc),file=sys.stderr);return 1
    return 0

if __name__=="__main__":raise SystemExit(main())
