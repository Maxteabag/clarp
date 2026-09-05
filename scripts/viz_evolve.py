#!/usr/bin/env python3
"""Let Astra develop actual visual source against a read-only fleet snapshot."""
import argparse,json,pathlib,sqlite3,sys,time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'))
from lib import db,viz_library,viz_normalize,viz_rule_author
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--db',type=pathlib.Path,required=True)
p.add_argument('--library',type=pathlib.Path,required=True)
p.add_argument('--reason',default='Reinvent the fleet map as a beautiful, living, hierarchical world. Develop real source software and new visual systems; do not just choose icons.')
p.add_argument('--snapshot',type=pathlib.Path)
a=p.parse_args()
con=sqlite3.connect(a.db.resolve().as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
db.conn=lambda:con;viz_library.path=lambda:a.library
world=viz_normalize.build_fleet_map(int(time.time()*1000)-3600000)['world'];con.close()
if a.snapshot:a.snapshot.write_text(json.dumps(world))
print(f'Developing against {len(world["events"])} events and {len(world["entities"])} evidenced entities',flush=True)
print(json.dumps(viz_rule_author.evolve_world(world,a.reason)),flush=True)
