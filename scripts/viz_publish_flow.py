#!/usr/bin/env python3
"""Publish the checked-in Flow source as a new Flow revision in a JSON library.

This is how a reviewed static/viz-flow iteration enters a learning library
without a Git checkout on the host: it keeps view_programs.world untouched,
records the reason, and refuses to run against the live library unless asked.
"""
import argparse
import json
import pathlib
import shutil
import sys
import time

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server'))
from lib import viz_library


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library',type=pathlib.Path,required=True,help='JSON library to update (a copy for previews)')
    p.add_argument('--source',type=pathlib.Path,default=ROOT/'static/viz-flow')
    p.add_argument('--reason',default='Publish reviewed Flow source: work objects, relationship vocabulary, validation and aging')
    p.add_argument('--allow-live',action='store_true',help='Permit the clarp-fleet-preview library path')
    a=p.parse_args()
    live=pathlib.Path.home()/'.local/share/clarp-fleet-preview/library.json'
    if a.library.resolve()==live.resolve() and not a.allow_live:
        p.error('refusing the live library without --allow-live; use a copy for previews')
    manifest=json.loads((a.source/'program.json').read_text())
    files={name:(a.source/name).read_text() for name in manifest['files']}
    program={'title':manifest['title'],'entry':manifest['entry'],'files':files,'view':'flow','notes':manifest.get('notes','')}
    viz_library.path=lambda:a.library
    current=viz_library.load()
    if current.get('program',{}).get('digest') and current['program'].get('files')==files:
        print('unchanged: library already carries this Flow source');return 0
    backup=a.library.with_name(a.library.stem+f'.before-flow-publish-{int(time.time())}.json')
    if a.library.exists():shutil.copy2(a.library,backup);print('backup',backup)
    updated=viz_library.apply_program(program,current['revision'],a.reason,current.get('scene_coverage',[]))
    print('published revision',updated['revision'],'digest',updated['program']['digest'][:12],'world preserved:',bool(updated['view_programs'].get('world')))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
