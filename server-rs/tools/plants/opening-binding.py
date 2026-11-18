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
    ('prune without retirement','retired_at.is_some_and(|retired| retired <= now)','true'),
]]
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
