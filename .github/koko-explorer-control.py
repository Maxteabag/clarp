#!/usr/bin/python3
"""One Explorer run per exact production ref; identical test-only geometry diagnostics."""
import hashlib,json,os,pathlib,subprocess,sys,time
control=pathlib.Path(__file__).resolve().parents[1]
root=control.parent;out=root/'explorer-control-raw';out.mkdir(mode=0o700)
refs={'baseline':'6da6d1ddcdcf6ad2b5e81402f71cf3fdee1d81fb','candidate':'998044a9c884ed9b4a3b0250b81d9331a262080e'}
files=['desktop-rs/Cargo.lock','desktop-rs/slint-app/src/explorer_checks.rs','desktop-rs/slint-app/ui/sidebar.slint','desktop-rs/slint-app/src/driver.rs','desktop-rs/slint-app/src/headless.rs','desktop-rs/tests/fake_host.py','desktop-rs/slint-app/tests/check.sh']
receipt={'order':['baseline','candidate'],'confounds':['Fixed order: candidate follows baseline; common target cache warms in this order. No reverse-order or repeated arm.','Identical test-only geometry logging and failure screenshot instrument both arms; may perturb timing.'],'viewport':'1280x800@1','checks_per_arm':1,'retries':0,'arms':{}}
receipt['compiler']=subprocess.check_output(['rustc','--version','--verbose'],text=True)
receipt['runner']={'os':os.environ.get('RUNNER_OS'),'arch':os.environ.get('RUNNER_ARCH'),'cpu_count':os.cpu_count(),'memory_bytes':os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')}
marker='''        let cursor = window.get_sidebar_cursor();
        check(cursor_in_view(window),'''
replacement='''        let cursor = window.get_sidebar_cursor();
        let agent_name = name_of(window, &cursor);
        eprintln!("EXPLORER_DIAG stage={} elapsed_ms={} cursor={} name={} row={:?} list={:?} focused={} chats={}",
            name, elapsed.as_millis(), cursor, agent_name, explorer_row(&agent_name), explorer_list(), window.get_sidebar_focused(), window.get_chats().row_count());
        if !cursor_in_view(window) {
            if let Ok(out) = std::env::var("CLARP_EXPLORER_DIAG_OUT") {
                shot(&out, "failed-cursor");
            }
        }
        check(cursor_in_view(window),'''
# Check both source bindings and all shared test/dependency inputs before compiling either arm.
for label,ref in refs.items():
 arm=root/label
 actual=subprocess.check_output(['git','-C',str(arm),'rev-parse','HEAD'],text=True).strip();assert actual==ref
 receipt['arms'][label]={'head':actual,'inputs':{f:hashlib.sha256((arm/f).read_bytes()).hexdigest() for f in files}}
for f in files:assert receipt['arms']['baseline']['inputs'][f]==receipt['arms']['candidate']['inputs'][f],f
for label in refs:
 arm=root/label;dest=out/label;dest.mkdir();src=arm/'desktop-rs/slint-app/src/explorer_checks.rs'
 original=src.read_text();assert original.count(marker)==1;src.write_text(original.replace(marker,replacement))
 diff=subprocess.check_output(['git','-C',str(arm),'diff','--binary']);(dest/'test-only.patch').write_bytes(diff)
 changed=subprocess.check_output(['git','-C',str(arm),'diff','--name-only'],text=True).splitlines();assert changed==['desktop-rs/slint-app/src/explorer_checks.rs']
 receipt['arms'][label]['instrumented_check_sha256']=hashlib.sha256(src.read_bytes()).hexdigest()
assert receipt['arms']['baseline']['instrumented_check_sha256']==receipt['arms']['candidate']['instrumented_check_sha256']
(out/'ADMISSION.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
shared=control/'desktop-rs/target'
env=dict(os.environ,CARGO_TARGET_DIR=str(shared),CARGO_BUILD_JOBS='2',CLARP_HEADLESS_SIZE='1280x800@1')
for label in refs:
 arm=root/label;dest=out/label;target=arm/'desktop-rs/target';assert not target.exists();target.symlink_to(shared,target_is_directory=True)
 before=time.monotonic()
 with open(dest/'build.log','wb') as log:
  build=subprocess.run(['cargo','build','--locked','-p','clarp-slint'],cwd=arm/'desktop-rs',env=env,stdout=log,stderr=subprocess.STDOUT)
 receipt['arms'][label]['build_rc']=build.returncode
 if build.returncode:
  (out/'TERMINAL.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n');sys.exit(build.returncode)
 receipt['arms'][label]['built_binary_sha256']=hashlib.sha256((shared/'debug/clarp-slint').read_bytes()).hexdigest()
 env['CLARP_EXPLORER_DIAG_OUT']=str(dest)
 with open(dest/'check.log','wb') as log:
  check=subprocess.run(['bash','slint-app/tests/check.sh','explorer',str(dest)],cwd=arm/'desktop-rs',env=env,stdout=log,stderr=subprocess.STDOUT)
 receipt['arms'][label].update(check_rc=check.returncode,elapsed_seconds=round(time.monotonic()-before,3))
(out/'TERMINAL.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n')
manifest={str(p.relative_to(out)):hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file()};(out/'MANIFEST.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
print(json.dumps(receipt,sort_keys=True))
sys.exit(1 if any(a['check_rc'] for a in receipt['arms'].values()) else 0)
