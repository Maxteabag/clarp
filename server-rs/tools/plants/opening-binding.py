"""Wrong rules for the release owner's R77 and R78 rulings."""
import runner

K='clarp-kernel/src/rings/binding.rs'
RUNS={'stories':['test','--test','opening_binding']}
PLANTS=[(K,'stories',label,old,new) for label,old,new in [
    ('ignore the agent of a turn','format!("{agent_id}:{turn_id}")','format!("any:{turn_id}")'),
    ('unknown turn is bound','turn_id != 0','turn_id != u64::MAX'),
    ('an unknown agent can bind','!agent_id.is_empty()','true'),
    ('reuse the new epoch','"epoch":self.epoch','"epoch":opening["epoch"]'),
    ('retirement leaves a clip playable','!opening["retired"].is_null()','false'),
    ('a replaced initiative may speak','intent["admission_id"] != self.admission_id','false'),
    ('prune just before the day is over','>= 86_400_000','>= 86_399_999'),
    ('keep an opening at its deadline','>= 86_400_000','> 86_400_000'),
    ('prune without retirement','let Some(retired) = retired_at else','let Some(retired) = retired_at.or(Some(0)) else'),
    ('count only from the call end','retired.max(ended)','ended'),
    ('count only from retirement','retired.max(ended)','retired'),
    ('keep retired records with no end forever','CallEnd::Unknown => retired','CallEnd::Unknown => return false'),
    ('prune while a call is live','CallEnd::Live => return false','CallEnd::Live => retired'),
]]
RUNS['reads']=['test','-p','clarp-host','--lib','ordinary_audio_reads_at_most_one_key']
RUNS['wire']=['test','--test','host','a_live_call_uses_its_frozen_team']
for file,run,label,old,new in [
    (K,'stories','a binding loses its call','"call_id":opening["call_id"]','"call_id":"lost"'),
    (K,'stories','swap the two opening causes','"kind":self.kind','"kind":"initiative"'),
    ('clarp-host/src/fleet.rs','reads','read the whole table before an ordinary clip','Some(key) => store.get(&shelf("opening_by_turn"), &key)','Some(key) => {store.list(&shelf("opening_by_turn"))?;store.get(&shelf("opening_by_turn"), &key)}'),
    ('clarp-host/src/calls/opening.rs','wire','ignore the opening retention period','if !clarp_kernel::rings::binding::expired','if clarp_kernel::rings::binding::expired'),
]:
    PLANTS.append((file,run,label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
