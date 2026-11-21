"""Wrong routes, display choices and model selection for an agent's portraits."""
import runner

RUNS = {
    'settings': ['test','--test','portrait_generation'],
    'host': ['test','--test','host','agent_portraits::'],
}
K = 'clarp-kernel/src/portraits.rs'
P = 'clarp-host/src/personas.rs'
H = 'clarp-host/src/edge.rs'
F = 'clarp-host/src/faces.rs'
PLANTS = [
    (K,'settings','keep surrounding spaces','s.trim().to_ascii_lowercase()', 's.to_ascii_lowercase()'),
    (K,'settings','case-sensitive stored value','s.trim().to_ascii_lowercase().as_str()', 's.trim()'),
    (K,'settings','ignore numeric true','"true" | "1"', '"true" | "2"'),
    (K,'settings','ignore yes','"yes" | "on"', '"off" | "on"'),
    (K,'settings','ignore on','"yes" | "on"', '"yes" | "no"'),
    (K,'settings','ignore true','"true" | "1"', '"false" | "1"'),
    (P,'host','wrong validation receipt','"model_avatars required"','"model required"'),
    (P,'host','save into another preference','clarp_kernel::portraits::MODEL_SETTING.to_owned()', '"other".to_owned()'),
    (H,'host','read a different snapshot preference','directory.setting(clarp_kernel::portraits::MODEL_SETTING)','directory.setting("other")'),
    (H,'host','use another backend default setting','format!("{}_model", view.backend)','format!("{}_effort", view.backend)'),
    (F,'host','omit configured model family','model_files(persona, backend, model, default_model)','model_files(persona, backend, model, "")'),
    (F,'host','janitor loses the generic portrait','"janitor-default.jpg".to_string()', '"absent.jpg".to_string()'),
    (F,'host','always show fallback janitor art','vec![file.to_string(), "janitor-default.jpg".to_string()]','vec!["janitor-default.jpg".to_owned()]'),
]
# The reference's pure comparisons locate differences in the capability, wire body and refusal.
G = 'clarp-kernel/src/portrait_generation.rs'
W = 'clarp-host/src/portrait_generation.rs'
J = 'clarp-host/src/fleet/portrait_jobs.rs'
E = 'clarp-host/src/pictures.rs'
RUNS['generation'] = ['test','--test','host','agent_portraits::the_owner_asks']
RUNS['outcomes'] = ['test','--test','host','agent_portraits::the_portrait_job_handles']
def change(file,run,items):
    for what,old,new in items:
        PLANTS.append((file,run,what,old,new))

change(G,'settings',[
    ('hash the trimmed identity','agent.as_bytes()', 'agent.to_lowercase().as_bytes()'),
    ('shorten job hash','&digest[..20]', '&digest[..19]'),
    ('extend job hash','&digest[..20]', '&digest[..21]'),
    ('change the reserved job name','"portrait-generation-{}"', '"portrait-edit-{}"'),
    ('refuse PNG','"image/png" | "image/jpeg"', '"image/gif" | "image/jpeg"'),
    ('refuse JPEG','"image/jpeg" | "image/webp"', '"image/gif" | "image/webp"'),
    ('refuse WebP','| "image/webp")','| "image/gif")'),
    ('unknown agent reason on GET','"Unknown Agent session"', '"unknown"'),
    ('missing key reason','"Configure an OpenAI API key on this Computer"', '"No key"'),
    ('miss the disabled account','else if !key','else if false'),
    ('no source reason','None => "Add or select a primary portrait first"', 'None => ""'),
    ('format refusal','"Use a PNG, JPEG, or WebP primary portrait"', '"format"'),
    ('source limit includes one extra byte','len > MAX_IMAGE as u64', 'len > MAX_IMAGE as u64+1'),
    ('refuse the last allowed source byte','len > MAX_IMAGE as u64', 'len >= MAX_IMAGE as u64'),
    ('wrong size reason','"Primary portrait is too large to generate from"', '"too large"'),
    ('occupied daily quota also refuses an active duplicate','!active && picture::admitted','picture::admitted'),
    ('allow the twenty-first launch','admitted(None, begun)','admitted(None, begun.saturating_sub(1))'),
    ('refuse launch twenty','admitted(None, begun)','admitted(None, begun.saturating_add(1))'),
    ('wrong capability shape','"portrait-generation.v1"','"portrait-generation.v0"'),
    ('invert available flag','"available":reason.is_empty()', '"available":!reason.is_empty()'),
    ('wrong billed provider','"provider":"openai"','"provider":"elsewhere"'),
    ('wrong edit count','"count":2','"count":1'),
    ('lose cost notice','Uses the configured OpenAI API account; image charges may apply.', 'Enabled.'),
    ('omit current job','"job":job','"job":null'),
    ('wrong candid prompt','Create an intimate, warm close-up ', 'Create a wide shot '),
    ('wrong everyday prompt','Create a natural everyday-life scene ', 'Create a candid close-up '),
    ('wrong posted model','("model", picture::MODEL)','("model", "model")'),
    ('lose the edited prompt','("prompt", prompt)','("prompt", "")'),
    ('wrong square size','("size", "1024x1024")','("size", "512x512")'),
    ('wrong edit quality','("quality", "medium")','("quality", "high")'),
    ('use a single image field','image[]','image'),
    ('change upload filename','filename=\\"portrait.png\\"','filename=\\"photo.png\\"'),
    ('drop reference bytes','body.extend_from_slice(source);','body.extend_from_slice(&[]);'),
    ('lose final multipart boundary','{boundary}--\\r\\n','{boundary}\\r\\n'),
    ('label all sources PNG','Content-Type: {mime}','Content-Type: image/png'),
    ('refuse a maximum sized result','bytes.len() <= MAX_IMAGE','bytes.len() < MAX_IMAGE'),
])
change(G,'outcomes',[
    ('accept identical edits','if first == second {','if false {'),
    ('reject every pair','if first == second {','if true {'),
    ('accept a non-image','Err("image provider returned invalid portrait content")','Ok("image/png")'),
    ('wrong duplicate reason','Err("image provider returned duplicate portrait alternatives")','Err("duplicate")'),
])
change(J,'generation',[
    ('use a different daily counter','self.computer_jobs.begun_since(\n            rules::KIND,','self.computer_jobs.begun_since(\n            "persona-avatar",'),
    ('exclude every prior launch','self.now_ms() - clarp_kernel::contacts::picture::DAILY_WINDOW_MS','self.now_ms() + clarp_kernel::contacts::picture::DAILY_WINDOW_MS'),
    ('register running before claiming','queued: true,','queued: false,'),
    ('make cancellation sticky','restart_cancelled: true,','restart_cancelled: false,'),
    ('keep the old alternates','portrait.sha256 == digest','true'),
])
change(J,'port',[
    ('change the identity of the retained primary','Some((digest, kind)),','Some(("absent", kind)),'),
])
change(J,'generation',[
    ('record a foreign generation','row.job.generation == generation','row.job.generation != generation'),
    ('accept only a cancelled job','row.job.status == JobStatus::Running','row.job.status == JobStatus::Cancelled'),
    ('omit asset link','Some(asset.asset_id.clone()),','None,'),
])
change(W,'generation',[
    ('ask the provider on restart','failed: Some("portrait_worker_interrupted".into()),','failed: None,'),
    ('skip recovery for running jobs','job.kind == rules::KIND && job.status.is_active()', 'job.kind == rules::KIND && job.status==JobStatus::Queued'),
    ('hand out the wrong job in the start response','StatusCode::ACCEPTED,\n        Json(','StatusCode::OK,\n        Json('),
    ('lose the source session','member.record.session.clone()','String::new()'),
])
change(E,'outcomes',[
    ('dispatch a generation in place of an edit','"{}/edits"','"{}/generations"'),
    ('treat non-success as a usable image','!response.status().is_success()','false'),
])
B = 'clarp-host/src/fleet/computer_jobs.rs'
RUNS['port']=['test','-p','clarp-host','--lib','a_portrait_pair_is_atomic']
RUNS['clock']=['test','-p','clarp-host','--lib','portrait_attempts_count']
change(B,'clock',[
    ('count another providers starts','r.job.kind == kind','r.job.kind != kind'),
    ('count already running events','r.job.status == clarp_kernel::jobs::JobStatus::Queued','r.job.status.is_active()'),
    ('exclude exactly one day ago','r.at_ms >= since','r.at_ms > since'),
    ('include events one millisecond too old','r.at_ms >= since','r.at_ms >= since-1'),
])
change(J,'port',[
    ('write receipt under the wrong key','publication.assets[0].asset_id.clone(),','publication.assets[1].asset_id.clone(),'),
    ('discard durable bytes','Some(raw.clone())','Some(Vec::new())'),
    ('omit the committed gallery decision','AGENTS.into(),','"absent".into(),'),
    ('stage bytes as receipts','shelf(BYTES), asset.asset_id.clone()','shelf(PENDING), asset.asset_id.clone()'),
    ('record a different job outcome','failed: None,','failed: Some("failure".into()),'),
    ('lose publication on the second port write','self.replaced(at, record, sent);','let _ = sent.send(());'),
])
change(W,'port',[
    ('clear neither of the two original sources','(BYTES, first.asset_id.clone()),','(PENDING, first.asset_id.clone()),'),
    ('keep the second original source after publication','(BYTES, second.asset_id.clone()),','(BYTES, first.asset_id.clone()),'),
    ('keep published pair in the replay queue','(PENDING, first.asset_id.clone()),','(BYTES, first.asset_id.clone()),'),
    ('lose the media journal projection','!publication\n            .assets','publication\n            .assets'),
])

change(G,'settings',[
    ('wrong response object member','answer["data"][0]["b64_json"]','answer["data"][0]["image"]'),
    ('refuse last permitted response byte','bytes.len() > MAX_RESPONSE','bytes.len() >= MAX_RESPONSE'),
    ('accept response over the cap','bytes.len() > MAX_RESPONSE','bytes.len() > MAX_RESPONSE+1'),
    ('misreport malformed JSON','.map_err(|_| "OpenAI image response was invalid".to_string())?', '.map_err(|_| "bad response".to_string())?'),
])
change(J,'generation',[
    ('wrong computed job title','Generate portraits for {}','Portraits for {}'),
    ('lose generation detail','Generating two character-consistent portrait alternatives','Generating portraits'),
    ('shorten worker heartbeat timeout','Some(300_000)','Some(30_000)'),
    ('stale queued generations stay active','"expire_queued":true','"expire_queued":false'),
])
change(W,'generation',[
    ('misidentify the publisher','format!("{}openai", rules::CREATED_BY_PREFIX)','"agent".into()'),
    ('lose alternative number in caption','Generated portrait alternative {index}','Generated portrait alternative'),
])
PLANTS.append((H,'generation','wrong feature contract','("agent_portraits", 1)', '("agent_portraits", 2)'))
RUNS['startup'] = ['test','--test','host','startup_keeps_unreadable_portrait_publications']
# NOT PLANTED, with its reason:
#   that a pending publication which is no Publication is skipped by `recover` (`app.fleet.unread(PENDING, &key);
#     continue;` after `serde_json::from_slice::<Publication>`): the kind is judged at the start against
#     `Publication` (`portrait_jobs::KEPT`), so the walk has moved every such record aside before `recover` reads,
#     and the arm never runs. It stays because a read must say what it does with a record that is not one (the lints
#     forbid unwrap), and to pass it by is harmless. The rule is planted where it now lives: the declaration.
change(J,'startup',[
    ('a pending publication that cannot be read is not judged at the start',
     'crate::kept::fits::<Publication>,',
     'crate::kept::any,'),
])
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
