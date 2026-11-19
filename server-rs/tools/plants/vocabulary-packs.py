#!/usr/bin/env python3
"""Wrong rules for vocabulary selection, administration and transcription diagnostics."""
import runner
RUNS={
    'vectors':['test','--test','vocabulary_packs'],
    'admin':['test','--test','host','vocabulary_packs_are_managed_compiled_recorded_and_heard'],
    'index':['test','-p','clarp-host','--lib','vocabulary::store::tests'],
    'audio':['test','-p','clarp-host','--lib','retained_audio_obeys_trace_age_and_count_limits'],
}
K='clarp-kernel/src/vocabulary.rs'
D='clarp-kernel/src/vocabulary/sources.rs'
S='clarp-kernel/src/vocabulary/book.rs'
T='clarp-host/src/vocabulary/store.rs'
C='clarp-host/src/vocabulary/compile.rs'
A='clarp-host/src/vocabulary/audio.rs'
PLANTS=[]

def change(file,run,why,old,new):
    PLANTS.append((file,run,why,old,new))

for why,old,new in [
        ('wrong default budget','&BUDGETS[0]','&BUDGETS[2]'),
        ('spaces in provider id prevent selection','provider.trim().to_lowercase()','provider.to_lowercase()'),
        ('model override is dropped','if !model.is_empty()','if model.is_empty()'),
        ('batch ink keeps a socket budget','base.capacity = 0;','base.capacity = 100;'),
        ('batch ink caps individual words','base.max_term_chars = None;','base.max_term_chars = Some(1);'),
        ('a lexical word is counted for every character','word && !in_word','word || !in_word'),
        ('the underscore is punctuation',"c == '_'","c == '-'"),
        ('byte count ignores utf8','text.len().div_ceil(3)','text.chars().count().div_ceil(3)'),
        ('tokens do not round up','text.len().div_ceil(3)','text.len() / 3'),
        ('highest rarity loses','reduce(f64::max)','reduce(f64::min)'),
        ('empty rarity differs','return 0.0;','return 0.5;'),
        ('ordinary words are ranked as rare','                0.05','                0.55'),
        ('identifiers lose their rarity','                0.98','                0.7'),
        ('numeric words change rank','                0.9\n','                0.7\n'),
        ('proper nouns are not rare','                0.85','                0.55'),
        ('long ordinary words change tier','word.len() >= 12','word.len() > 12'),
        ('score ignores recent use','term.recency * 2.0','term.recency * 0.0'),
        ('priority is inverted','* pack.priority.max(0.0)','/ pack.priority.max(0.01)'),
        ('duplicate reason changes','"duplicate"','"already seen"'),
        ('term rejected exactly at threshold','t.rarity < self.floor','t.rarity <= self.floor'),
        ('confusable terms obey rarity floor','&& !t.confusable','&& t.confusable'),
        ('a guaranteed term is filtered','if !guaranteed','if guaranteed'),
        ('rarity rejection has wrong wording','"below rarity floor"','"below floor"'),
        ('term cap removes one extra character','python::first(&t.text, max)','python::first(&t.text, max - 1)'),
        ('token separator costs nothing','estimate_tokens(&fitted.text) + 1','estimate_tokens(&fitted.text)'),
        ('an exact fit is refused','self.used + cost > self.budget.capacity','self.used + cost >= self.budget.capacity'),
        ('overflow reason changes','"over budget"','"too long"'),
        ('cost records a constant instead of used units','self.used += cost','self.used += 1'),
        ('zero capacity gets wrong floor','..=0 => 1.0','..=0 => 0.9'),
        ('tight floor threshold off by one','1..120 => 0.65','1..119 => 0.65'),
        ('medium floor threshold off by one','120..300 => 0.45','120..299 => 0.45'),
        ('large floor threshold off by one','300..800 => 0.25','300..799 => 0.25'),
        ('disabled packs compete','p.enabled && !p.terms.is_empty()','!p.terms.is_empty()'),
        ('empty budget has wrong reason','"no budget"','"disabled"'),
        ('floor phase goes in reverse priority','b.priority.total_cmp(&a.priority)','a.priority.total_cmp(&b.priority)'),
        ('lowest ranked floor terms go first','rank(b, pack).total_cmp(&rank(a, pack))','rank(a, pack).total_cmp(&rank(b, pack))'),
        ('floor size is always zero','take(pack.floor.max(0) as usize)','take(0)'),
        ('globally worst terms selected first','b.0.total_cmp(&a.0)','a.0.total_cmp(&b.0)'),
        ('global selection bypasses rarity','selection.admit(t, false)','selection.admit(t, true)'),
        ('floor terms do not bypass rarity','selection.admit(t, true)','selection.admit(t, false)'),
        ('chosen terms reversed twice','words.reverse();','words.reverse(); words.reverse();'),
        ('prose starts a token too late','budget.capacity >= 300','budget.capacity > 300'),
        ('prose heading changes','Terms that may come up:','The following terms:'),
        ('rendered short prompt loses period','format!("{text}.")','format!("{text}")'),
        ('headroom is unused capacity plus cost','capacity-selection.used','capacity+selection.used'),
        ('judge discards only above threshold','value >= 0.6','value > 0.6'),
        ('judge keeps a transcript just below threshold incorrectly','value >= 0.6','value >= 0.5999')]:
    change(K,'vectors',why,old,new)

for why,old,new in [
        ('correction generator uses heard spelling','python::strip(intended)','python::strip(heard)'),
        ('corrections not confusable','t.confusable = true','t.confusable = false'),
        ('correction priority changes','"corrections", 3.0, 2','"corrections", 2.0, 2'),
        ('correction floor changes','3.0, 2, unique(corrections)','3.0, 1, unique(corrections)'),
        ('recent speech limit reduced','recent.truncate(24)','recent.truncate(23)'),
        ('recent speech limit increased','recent.truncate(24)','recent.truncate(25)'),
        ('recent speech priority changes','"recent-speech", 2.5','"recent-speech", 1.5'),
        ('recency grows in older transcripts','1.0 - i as f64','1.0 + i as f64'),
        ('agent floor disappears','"agents", 2.0, 2','"agents", 2.0, 0'),
        ('workspace priority changes','"workspace", 1.2','"workspace", 1.1'),
        ('project rarity changes','t.rarity.max(0.95)','t.rarity.max(1.0)'),
        ('branch phrases have one fewer word','.take(6)','.take(5)'),
        ('deduplication is case sensitive','seen.insert(python::casefold(&t.text))','seen.insert(t.text.clone())'),
        ('identifier character cap increases','t.len() < 4','t.len() < 5'),
        ('library names enter vocabulary','low.starts_with("lib")','low.starts_with("absent")'),
        ('workspace ranking inverts frequency','std::cmp::Reverse(r.0)','r.0')]:
    change(D,'vectors',why,old,new)

for why,old,new in [
        ('shortest valid trace refused','(8..=64)','(9..=64)'),
        ('longest valid trace refused','(8..=64)','(8..=63)'),
        ('count limit loses one clip','i >= COUNT','i >= COUNT - 1'),
        ('count limit keeps one too many','i >= COUNT','i > COUNT'),
        ('clip removed exactly at age limit','unwrap_or_default() > AGE','unwrap_or_default() >= AGE'),
        ('age limit grows by a second','14 * 86400','14 * 86400 + 1')]:
    change(A,'audio',why,old,new)

for why,old,new in [
        ('pack duplicate is a missing pack','if self.packs.values().any(|p| p.name == name) {\n                        return Err(conflict','if self.packs.values().any(|p| p.name == name) {\n                        return Err(missing'),
        ('pack priority default changes','number(&v["priority"], 1.0)','number(&v["priority"], 2.0)'),
        ('pack floor default changes','integer(&v["floor"], 0)','integer(&v["floor"], 1)'),
        ('wrong required name','bad("name is required")','bad("missing name")'),
        ('wrong workspace response field','"harvests_workspace":enabled','"harvest_workspace":enabled'),
        ('added flag reversed','response["added"] = json!(self.add','response["added"] = json!(!self.add'),
        ('term update flag reversed','response["updated"] = json!(updated)','response["updated"] = json!(!updated)'),
        ('reading a profile returns a different pack name','"name":pack.name','"name":pack.kind'),
        ('pack readings lose term counts','"terms":p.terms.len()','"count":p.terms.len()'),
        ('term listing changes order','b.rarity.total_cmp(&a.rarity).then_with','a.rarity.total_cmp(&b.rarity).then_with'),
        ('assigned team is hidden','"profile_name":self.profiles[&a.profile_id].name,"agent_id":a.agent_id,"team_id":a.team_id','"profile_name":self.profiles[&a.profile_id].name,"agent_id":a.agent_id,"team_id":""')]:
    if old!=new:change(S,'vectors',why,old,new)
for why,old,new in [
        ('preview forgets profile','audit["profile_id"] = json!(pf)','audit["profile_id"] = Value::Null'),
        ('stream reports another engine','model: run.model.into(),','model: "wrong-model".into(),')]:
    change(C,'admin',why,old,new)

J='clarp-host/src/vocabulary/judge.rs'
for why,old,new in [
        ('judge uses wrong model','"model":"jev-latest"','"model":"jev-other"'),
        ('judge receives text under another key','"state":{"transcript":text}','"state":{"speech":text}'),
        ('wrong judgment question id','"questions":{"junk":','"questions":{"other":'),
        ('billing refusal has the wrong hold','if billing { 3600 } else { 120 }','if billing { 120 } else { 120 }'),
        ('billing error does not open the breaker','if billing || b.failures >= 3','if !billing && b.failures >= 3'),
        ('lowest timeout refused','(100..=30000)','(101..=30000)'),
        ('highest timeout refused','(100..=30000)','(100..=29999)'),
        ('timeout is not saved','"judgments.timeout_ms".into(), n.to_string()','"judgments.other".into(), n.to_string()'),
        ('enabling the judge is not saved','"judgments.enabled".into(),','"judgments.other_enabled".into(),'),
        ('wrong settings refusal','"timeout_ms must be an integer"','"expected integer"'),
        ('wrong site refusal','"sites must be an object"','"expected an object"'),
        ('an unanswered call never falls back','let fallback = || clarp_kernel::heard::kept(&text);','let fallback = || text.clone();')]:
    change(J,'admin',why,old,new)

for why,old,new in [
        ('wrong kind refusal','"kind must be static or dynamic"','"unknown kind"'),
        ('pack response loses its id','response["pack_id"] = json!(id);','response["other_id"] = json!(id);'),
        ('wrong profile duplication refusal','"a profile with that name exists"','"duplicate profile"'),
        ('term text limit bypasses update clamp','n.clamp(0.0, 1.0)','n.clamp(0.0, 2.0)'),
        ('wrong assignment owner refusal','"assign to exactly one of agent_id or team_id"','"assign to an owner"'),
        ('workspace flag accepts only false','let enabled = v["enabled"]\n                    .as_bool()','let enabled = v["enabled"]\n                    .as_bool().filter(|b| !*b)'),
        ('unassign refusal changes','"agent_id or team_id is required"','"an owner is required"'),
        ('term deletion leaves the term','p.terms.retain(|t| t.term_id != id);','p.terms.retain(|t| t.term_id == id);'),
        ('pack membership positions move','integer(&v["position"], 0)?','integer(&v["position"], 0)? + 1'),
        ('workspace flag is not persisted','.harvests_workspace = enabled;','.harvests_workspace = !enabled;'),
        ('pack deletion leaves an empty pack','self.packs.remove(&pk);','self.packs.get_mut(&pk).map(|p| p.terms.clear());'),
        ('profile deletion leaves an empty profile','self.profiles.remove(&pf);','self.profiles.get_mut(&pf).map(|p|p.members.clear());')]:
    change(S,'vectors',why,old,new)
for why,old,new in [
        ('terms ordered by lowest rarity first','ORDER BY rarity DESC,term_id','ORDER BY rarity ASC,term_id'),
        ('workspace never contributes names','if profile.as_ref().is_some_and(|(_, yes)| *yes)','if profile.as_ref().is_some_and(|(_, yes)| !yes)'),
        ('preview reports another session','audit["session"] = json!(self.session)','audit["session"] = Value::Null')]:
    if why!='terms ordered by lowest rarity first':change(C,'admin',why,old,new)
for why,old,new in [
        ('audio result has another MIME type','header("content-type", kind)','header("content-type", "application/octet-stream")'),
        ('range responses return a full-response status','status(StatusCode::PARTIAL_CONTENT)','status(StatusCode::OK)'),
        ('invalid trace has the wrong refusal','"no retained audio for that trace"','"no clip"')]:
    if why not in ('invalid trace has the wrong refusal',):change(A,'admin',why,old,new)

for why,old,new in [
        ('insertion ignores spelling case only on exact matches','t.text.eq_ignore_ascii_case(word)','t.text == word'),
        ('renaming can collide with a differently cased term','t.text.eq_ignore_ascii_case(name)','t.text == name'),
        ('a duplicate insertion uses no term id','self.last_term += 1;','if !p.terms.iter().any(|t|t.text.eq_ignore_ascii_case(word)) {self.last_term += 1;}'),
        ('renaming a pack accepts a conflicting name','pk != &id && p.name == name','pk == &id && p.name == name'),
        ('pack deletion leaves memberships','for p in self.profiles.values_mut() {\n                    p.members.remove(&pk);','for p in self.profiles.values_mut() {\n                    p.members.remove("");'),
        ('profile deletion leaves assignments','self.assignments.retain(|a| a.profile_id != pf)','self.assignments.retain(|a| a.profile_id == pf)'),
        ('replacement loses unrelated assignments','agent.is_empty() || a.agent_id != agent','!agent.is_empty() && a.agent_id != agent'),
        ('reassignment keeps an old team binding','team.is_empty() || a.team_id != team','team.is_empty() || a.team_id == team'),
        ('new profile has workspace harvesting on','harvests_workspace: false,','harvests_workspace: true,'),
        ('static packs contribute no terms','p.kind == "static"','p.kind == "dynamic"'),
        ('terms are stored without pronunciation','say_as: text(&v["say_as"]).trim().into()','say_as: String::new()'),
        ('pack enabled update is inverted','p.enabled = crate::python::truthy(x)','p.enabled = !crate::python::truthy(x)'),
        ('assignment to both owners is accepted','a.is_empty() == t.is_empty()','a.is_empty() && t.is_empty()')]:
    change(S,'vectors',why,old,new)

for why,old,new in [
        ('completed transcript is lost','r.transcript = transcript;','r.transcript.clear();'),
        ('counter reservations are not persisted','COUNTER, "last".into(), encode(&id)?','COUNTER, "last".into(), encode(&(id - 1))?'),
        ('the run is written under the following id','id.to_string(), encode(&record)?','(id+1).to_string(), encode(&record)?'),
        ('a restarted host reads another session index','load::<Session>(fleet, SESSIONS, s)','load::<Session>(fleet, SESSIONS, "other")'),
        ('the run lookup drops the trace index','TRACES, record.trace.clone(), encode(&id)?','TRACES, String::new(), encode(&id)?'),
        ('a clip reads and rewrites the whole administrative book while reserving its number',
         'let id = last.checked_add', 'let book:Book=load(fleet,BOOK,"book").await?.unwrap_or_default(); fleet.keep(BOOK,"book".into(),Some(encode(&book)?)).await.map_err(|_| unavailable())?; let id = last.checked_add'),
        ('number reservation overwrites the book','COUNTER, "last".into(), encode(&id)?','BOOK, "book".into(), encode(&id)?'),
        ('per-session lookup is never written','(SESSIONS, record.session.clone(), encode(&session)?),', ''),
        ('all-sessions lookup is never written','(GLOBAL, "all".into(), encode(&all)?),', ''),
        ('trace lookup is never written','writes.push((TRACES, record.trace.clone(), encode(&id)?));', 'let _ = &record.trace;'),
        ('session page grows with every run','remember(&mut session.newest, id, PAGE)','remember(&mut session.newest, id, usize::MAX)'),
        ('global page grows with every run','remember(&mut all, id, PAGE)','remember(&mut all, id, usize::MAX)'),
        ('session drops its 200th run','remember(&mut session.newest, id, PAGE)','remember(&mut session.newest, id, PAGE-1)'),
        ('global page drops its 200th run','remember(&mut all, id, PAGE)','remember(&mut all, id, PAGE-1)'),
        ('last spoken run is ordered by completion time','ids.partition_point(|previous| *previous > id)','0'),
        ('one older spoken run is discarded','const RECENT: usize = 3','const RECENT: usize = 2'),
        ('one extra spoken run is read','const RECENT: usize = 3','const RECENT: usize = 4'),
        ('recent results include silent clips','if !record.transcript.is_empty()','if record.transcript.is_empty()'),
        ('finish does not update the spoken index','writes.push((SESSIONS, r.session.clone(), encode(&session)?));', 'let _ = &session;'),
        ('the clip loads every audit to allocate a number','let id = last.checked_add','let _ = fleet.kept_all(RUNS).await; let id = last.checked_add'),
        ('trace lookup committed outside the run transaction','writes.push((TRACES, record.trace.clone(), encode(&id)?));','fleet.keep(TRACES,record.trace.clone(),Some(encode(&id)?)).await.map_err(|_| unavailable())?;'),
        ('spoken lookup committed outside clip completion','writes.push((SESSIONS, r.session.clone(), encode(&session)?));','fleet.keep(SESSIONS,r.session.clone(),Some(encode(&session)?)).await.map_err(|_| unavailable())?;')]:
    change(T,'index',why,old,new)

# NOT PLANTED: the former mutations for spending a listing slot on a missing
# run, accepting an unfinished spoken entry, returning a store error for a
# dangling trace, and publishing a run before its indexes all depended on
# separate writes. The two split-transaction mutations exercise that risk now.

if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
