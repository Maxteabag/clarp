#!/usr/bin/env python3
"""Check model selection, the bounded transfer, job fencing, and recovery at each cut."""
import runner

RUNS = {
    "catalog": ["test", "--test", "transcription_install"],
    "upload": ["test", "--test", "host", "the_app_installs_a_pinned_model"],
    "restore": ["test", "--test", "host", "installer_restart_reconciles_published_files"],
    "port": ["test", "-p", "clarp-host", "--lib", "a_model_is_registered_only_with_its_successful"],
    "offline": ["test", "--test", "host", "the_download_adapter_bounds_and_verifies_offline_streams"],
    "open": ["test", "-p", "clarp-host", "--lib", "an_install_in_the_job_book_never_starts"],
}
PLANTS = []
K = "clarp-kernel/src/heard/inventory.rs"
H = "clarp-host/src/heard/install.rs"
F = "clarp-host/src/heard/staging.rs"
D = "../tools/transcription-download.py"


def changes(file, run, pairs):
    for old, new in pairs:
        PLANTS.append((file,run,f"{old} becomes {new}",old,new))


for revision in ("3d3d5dee26484f91867d81cb899cfcf72b96be6c", "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
                 "536b0662742c02347bc0e980a01041f333bce120", "08e178d48790749d25932bbc082711ddcfdfbc4f",
                 "4df90f75321148c3a29a9e2351b7ddf8f5b115a8"):
    changes(K,"catalog",[(revision,"0000000000000000000000000000000000000000")])
for repo in ("Systran/faster-whisper-base.en", "Systran/faster-whisper-small.en", "Systran/faster-whisper-small",
             "Systran/faster-whisper-medium","deepdml/faster-whisper-large-v3-turbo-ct2"):
    changes(K,"catalog",[(f'"{repo}"','"other/unused"')])
changes(K,"catalog",[
    ("self.bytes / 4","self.bytes / 5"), ("256 * 1024 * 1024","255 * 1024 * 1024"),
    ("free >= remaining","free > remaining"), ("free.checked_sub(reserved)","Some(free)"),
    ("id.split_once(':')", "id.split_once('/')"),
])
changes(K,"catalog",[
    ("age > std::time::Duration::from_secs", "age >= std::time::Duration::from_secs"),
    ("7 * 24 * 3600", "8 * 24 * 3600"),
    ("Some(JobStatus::Cancelled) => true", "Some(JobStatus::Cancelled) => false"),
    ("Some(_) => false", "Some(_) => true"),
])
changes(D,"offline",[
    ('part in ("", ".", "..")','part in (".", "..")'), ('part in ("", ".", "..")','part in ("", "..")'),
    ('part in ("", ".", "..")','part in ("", ".")'), ("entry.st_nlink == 1","entry.st_nlink == 2"),
    ("total > maximum","total >= maximum"), ("total > maximum","total < 0"),
    ("size > entries[name][0]","size < entries[name][0]"),
    ('if offset < size:', 'if offset <= size:'),('if offset < size:', 'if offset > size:'),
    ("while chunk := output.read(BLOCK):\n                hashed.update(chunk)", "while chunk := output.read(BLOCK):\n                hashed.update(b'x')"),
    ("if response.status_code == 200:", "if response.status_code == 201:"),
    ('response.headers.get("Content-Range") !=','response.headers.get("Content-Range") =='),
    ("if offset != size:", "if offset == size:"), ("if hashed.hexdigest() != checksum:", "if hashed.hexdigest() == checksum:"),
    ("output.truncate(0)", "output.flush()"), ("offset > size or transferred > maximum","offset >= size or transferred > maximum"),
    ("hashlib.sha256() if lfs","hashlib.sha1() if lfs"), ('f"blob {size}\\0"','f"blob {size}:"'),
])
changes("../tools/whisper-worker.py","offline",[("local_files_only=True","local_files_only=False")])
changes(H,"upload",[
    ('"{KIND}-{}"','"{KIND}--{}"'), ("[..16]","[..15]"),
    ('"job_generation":job.generation','"job_generation":job.generation + 1'),
    ('"job_handle":job.handle()', '"job_handle":job.id'), ('"status":"installing"', '"status":"queued"'),
    ('"status":"installed"', '"status":"ready"'), ('"provisional_id"', '"pending_id"'),
    ('ids::hex(16)','ids::hex(8)'), ('StatusCode::ACCEPTED','StatusCode::OK'),
    ('"model installation is still in progress: {id}"','"model still in progress: {id}"'),
    ('!change.generation_mismatch','change.generation_mismatch'),
    ('r.generation == job.generation','r.generation != job.generation'),
    ('"Model installation cancelled"','"cancelled"'),
    ('row["status"] = json!("installing")','row["status"] = json!("available")'),
    ('row["status"] = json!("failed")','row["status"] = json!("available")'),
    ('folders.staging(true)','folders.staging(false)'), ('maximum_bytes: maximum','maximum_bytes: 0'),
    ('restart_cancelled: true','restart_cancelled: false'),
    ('source.repository,','"other/snapshot",'), ('revision: source.revision','revision: "unreviewed"'),
    ('if !models::space_available','if models::space_available'),
])
changes(H,"restore",[
    ('filter(|j| j.status.is_active())','filter(|j| j.status.is_terminal())'),
    ('job.status == JobStatus::Queued','job.status != JobStatus::Queued'),
    ('"worker_vanished"','"worker_disappeared"'),
    ('job.generation > intent.generation','job.generation < intent.generation'),
    ('intent.device, intent.inode','intent.inode, intent.device'),
    ('fleet.keep(INTENTS, id.into(), None).await?;\n        Ok(())','let _ = fleet;\n        Ok(())'),
])
changes(F,"restore",[
    ('format!(".install-{model}")','format!(".download-{model}")'),
    ('(device, inode)','(inode, device)'), ('self.rename(&self.model, &self.stage)','self.rename(&self.stage, &self.model)'),
    ('std::fs::remove_dir_all(beneath(&self.root, &self.stage))?', 'let _ = &self.root'),
])
changes("clarp-host/src/heard/inventory.rs","upload",[
    ('row["_local_path"] = json!(path)','row["_local_path"] = json!("")'),
    ('row["_managed_root"] = json!(path)','row["_managed_root"] = Value::Null'),
])
changes("clarp-host/src/fleet/computer_jobs.rs","port",[
    ('writes.push((TABLE.into(), job.id.clone(), Some(bytes)))', 'writes = vec![(TABLE.into(), job.id.clone(), Some(bytes))]'),
])
changes("clarp-host/src/heard/inventory.rs","upload",[
    ('("transcription-models", "records".into(), Some(bytes))','("transcription-models", "unused".into(), Some(bytes))'),
    ('("model-publication", id.into(), None)','("model-publication", id.into(), Some(b"invalid".to_vec()))'),
])
changes("clarp-host/src/heard/install.rs","open",[
    ('"worker_never_started"', '"installer_not_started"'),
])
if __name__=="__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
