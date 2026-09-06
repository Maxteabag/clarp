#!/usr/bin/env python3
"""Explicitly select a checked-in view as the author's baseline, preserving other views."""
import argparse,json,pathlib,sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from lib import viz_library
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--library',type=pathlib.Path,required=True)
p.add_argument('--view',choices=['world','flow'],required=True)
p.add_argument('--reason',required=True)
p.add_argument('--apply',action='store_true',help='Publish; default only reports the proposed switch')
a=p.parse_args();viz_library.path=lambda:a.library
library=viz_library.load();root=pathlib.Path(__file__).resolve().parents[1]/('static/viz-'+a.view)
manifest=json.loads((root/'program.json').read_text());program={**manifest,'view':a.view,'files':{n:(root/n).read_text() for n in manifest['files']}}
print(json.dumps({'from':(library.get('program') or {}).get('view','world'),'to':a.view,'revision':library['revision'],'files':list(program['files']),'apply':a.apply}))
if a.apply:
    updated=viz_library.apply_program(program,library['revision'],a.reason)
    print(json.dumps({'revision':updated['revision'],'preserved_views':list(updated['view_programs'])}))
