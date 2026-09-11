"""Build the self-contained explainer from the exact shipped renderer and evidence captures."""
from pathlib import Path
import argparse,base64,json,subprocess
root=Path(__file__).resolve().parent;repo=root.parents[2]
def jpeg(path):
    # Format conversion only; retain complete original screenshots, without crop.
    out=subprocess.check_output(['magick',str(path),'-quality','85','jpeg:-'])
    return 'data:image/jpeg;base64,'+base64.b64encode(out).decode()
pictures={'AFTER':'/var/tmp/fleet-map-test/lantern-live-proof/work-outcome-zoom.png','BEFORE':'/var/tmp/fleet-map-test/mappy-live-work-recheck/work-outcome-zoom.png','UNRESOLVED':'/var/tmp/mappy-s2/shots/real-unresolved.png','PREVIEW':'/var/tmp/fleet-map-test/explainer-preview.png'}
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--renderer-ref',default='8c38b99')
for token,path in pictures.items():parser.add_argument('--'+token.lower(),default=path)
args=parser.parse_args()
pictures={token:getattr(args,token.lower()) for token in pictures}
html=(root/'page.html').read_text()
for token,path in pictures.items():html=html.replace('@@'+token+'@@',jpeg(path))
modules=[]
for name in ['model-util.js','slate.js','lantern.js']:
    source=subprocess.check_output(['git','show',args.renderer_ref+':static/viz-flow/'+name],cwd=repo,text=True)
    modules.append(json.dumps('./'+name)+':(module,exports,require)=>{\n'+source+'\n}')
js='const factories={'+',\n'.join(modules)+'};const cache={};function load(name){if(cache[name])return cache[name].exports;const m={exports:{}};cache[name]=m;factories[name](m,m.exports,load);return m.exports;}'
html=html.replace('@@MODULES@@',js)
assert '@@' not in html
(root/'index.html').write_text(html)
print(root/'index.html')
