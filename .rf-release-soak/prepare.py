#!/usr/bin/env python3
"""Read-only Git export/freeze, not compute admission or release approval."""
import argparse, hashlib, io, json, shutil, subprocess, tarfile, re
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PROGRAM=Path('/var/tmp/clarp-refactor-program')
TOOL=PROGRAM/'release-tooling/v8n2-r1'
DIGEST='cad642ea27167e355dd570c4ab3c6f11bc83b636b845c983967aaf1cd4f56870'

def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
 return h.hexdigest()

def files(root):
 result={}
 for path in sorted(root.rglob('*')):
  if path.is_file():
   if root.resolve() not in path.resolve().parents:raise ValueError('archive symlink escapes capsule')
   result[path.relative_to(root).as_posix()]=sha(path)
 return result

def git(*args):return subprocess.check_output(['git',*args])

def export(pin,path):
 if len(pin)!=40 or any(c not in '0123456789abcdef' for c in pin):raise ValueError('full40-char Git pin required')
 if path.exists():raise ValueError('capsule already exists; reconcile before replacement')
 path.mkdir(parents=True)
 omitted=[]
 # Match heavy-remote's data boundary: only tracked source, no database/env or
 # transcript-shaped fixtures. Omissions must be tests/benchmarks, never product.
 with tarfile.open(fileobj=io.BytesIO(git('archive',pin))) as archive:
  members=[]
  for member in archive.getmembers():
   name=member.name
   if re.search(r'\.(sqlite3?|db|jsonl|sqlite-wal|sqlite-shm|sqlite-journal)$|(^|/)\.env$',name):
    if not name.startswith(('tests/','benchmarks/')):raise ValueError('product data-shaped source needs explicit review: '+name)
    omitted.append(name)
   else:members.append(member)
  archive.extractall(path,members=members,filter='data')
 bundle=None
 for candidate in sorted((PROGRAM/'bundles').iterdir(),key=lambda p:p.stat().st_mtime,reverse=True):
  r=subprocess.run(['git','diff','--quiet',candidate.name,pin,'--','web','package.json','package-lock.json','vite.config.js'])
  if r.returncode==0:bundle=candidate/'app';break
  if r.returncode!=1:raise RuntimeError('bundle ancestry query failed')
 if bundle is None:raise RuntimeError('no exact-input cached bundle; buildbox build needs separate receipt')
 shutil.copytree(bundle,path/'canary-bundle')
 shutil.copytree(TOOL/'canary/native',path/'canary-native')
 shutil.copy2(TOOL/'bin/release-check',path/'release-check.py')
 tree_files=files(path)
 packed=ROOT/'capsules'/(path.name+'.tar')
 if packed.exists():raise ValueError('packed capsule exists; reconcile before replacement')
 with tarfile.open(packed,'w') as archive:archive.add(path,arcname='.')
 archive_hash=sha(packed)
 shutil.rmtree(path)
 return {'sha':pin,'git_tree':git('rev-parse',pin+'^{tree}').decode().strip(),'path':str(Path('.rf-release-soak/capsules/unpacked')/path.name),'archive':str(packed.relative_to(ROOT.parent)),'archive_sha256':archive_hash,'sha256':tree_files,'bundle_source':bundle.parent.name,'omitted_data_fixtures':omitted}


def main():
 p=argparse.ArgumentParser();p.add_argument('--base',required=True);p.add_argument('--candidate',required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--gates',type=Path);p.add_argument('--rehearsal',action='store_true');a=p.parse_args()
 if sha(TOOL/'SHA256SUMS')!=DIGEST:raise RuntimeError('803 exact tool manifest mismatch')
 for line in (TOOL/'SHA256SUMS').read_text().splitlines():
  h,path=line.split('  ',1)
  if sha(TOOL/path)!=h:raise RuntimeError('tool hash mismatch')
 gates=json.loads(a.gates.read_text()) if a.gates else {}
 if not a.rehearsal and not gates:raise RuntimeError('freeze/gate/admission receipts required; no invented approval')
 capsule=ROOT/'capsules/staging'
 result={'phase':'full','job_id':'rf-releasec-bigmerge1-'+('rehearsal' if a.rehearsal else 'soak-800'),'owner_session':'rf-releasec-29c5','synthetic_data_only':True,'rehearsal':a.rehearsal,'tool_manifest_sha256':DIGEST,'soak_seconds':15 if a.rehearsal else 21600,'hard_total_seconds':7200 if a.rehearsal else 30000,'output':str(Path('.rf-release-soak')/'results'/('rehearsal' if a.rehearsal else 'bigmerge1')),'container':'clarp-soak-rf-releasec-'+('rehearsal' if a.rehearsal else '800'),'gate_receipts':gates,'payloads':{}}
 for role,pin in [('base',a.base),('candidate',a.candidate)]:result['payloads'][role]=export(pin,capsule/role)
 result['harness_sha256']={path.name:sha(path) for path in sorted(ROOT.iterdir()) if path.is_file() and path.suffix in ('.py','.sh')}
 with a.manifest.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
 print(json.dumps({'manifest':str(a.manifest),'manifest_sha256':sha(a.manifest),'rehearsal':a.rehearsal,'base':a.base,'candidate':a.candidate,'tool_manifest':DIGEST,'admission':'NOT ADMITTED'}))
if __name__=='__main__':main()
