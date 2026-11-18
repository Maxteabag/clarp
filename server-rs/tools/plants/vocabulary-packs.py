#!/usr/bin/env python3
"""Wrong rules for vocabulary selection, administration and transcription diagnostics."""
import runner
RUNS={
    'vectors':['test','--test','vocabulary_packs'],
    'admin':['test','--test','host','vocabulary_packs_are_managed_compiled_recorded_and_heard'],
    'audio':['test','-p','clarp-host','--lib','retained_audio_obeys_trace_age_and_count_limits'],
}
K='clarp-kernel/src/vocabulary.rs'
D='clarp-kernel/src/vocabulary/sources.rs'
S='clarp-host/src/vocabulary/store.rs'
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
        ('pack duplicate is a missing pack', '(StatusCode::CONFLICT, "a pack with that name exists".into())','(StatusCode::NOT_FOUND, "no such pack".into())'),
        ('pack priority default changes','number(&v["priority"], 1.0)','number(&v["priority"], 2.0)'),
        ('pack floor default changes','integer(&v["floor"], 0)','integer(&v["floor"], 1)'),
        ('wrong required name','bad("name is required")','bad("missing name")'),
        ('wrong workspace response field','"harvests_workspace":enabled','"harvest_workspace":enabled'),
        ('added flag reversed','Ok(changed > 0)','Ok(changed == 0)'),
        ('term update flag reversed','"updated":updated','"updated":!updated'),
        ('reading a profile returns a different pack name',"'position',position,'name',name","'position',position,'name',kind"),
        ('pack readings lose term counts',"'terms',(SELECT count(*)","'count',(SELECT count(*)"),
        ('term listing changes order','ORDER BY rarity DESC,text COLLATE NOCASE','ORDER BY rarity ASC,text COLLATE NOCASE'),
        ('assigned team is hidden',"'team_id',coalesce(team_id,'')","'team_id',coalesce(team_id,'')")]:
    if old!=new:change(S,'admin',why,old,new)
for why,old,new in [
        ('preview forgets profile','audit["profile_id"] = json!(pf)','audit["profile_id"] = Value::Null'),
        ('run neglects transcript result','SET transcript=?,latency_ms=?','SET transcript=upper(?),latency_ms=?'),
        ('stream reports another engine','run.model,','"wrong-model",')]:
    change(C,'admin',why,old,new)

if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
