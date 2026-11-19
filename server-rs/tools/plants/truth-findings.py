"""Wrong rules for what the differences page found: the actions under four doors, a plan's updater, the words of a janitor that cannot run."""
import runner

K='clarp-kernel/src/doors.rs'
S='clarp-host/src/screens.rs'
RUNS={
    'doors':['test','--test','action_doors_vectors'],
    'wire':['test','--test','host','a_change_the_python_host_has_a_route_for_is_not_built_here'],
}
PLANTS=[]
for path in ['/backend-auth/login','/backend-auth/login-code','/backend-auth/logout','/harness-providers/login',
             '/harness-providers/login-code','/harness-providers/logout','/agent-goal/pause','/agent-goal/resume',
             '/agent-goal/clear','/oracle/calls','/oracle/calls/close','/oracle/v2/calls','/oracle/v2/calls/close',
             '/oracle/v2/calls/control','/oracle/contact','/oracle/voice-provider','/oracle/connect',
             '/oracle/handoffs/ack','/oracle/delegations','/oracle/delegations/ack','/oracle/delegations/cancel']:
    PLANTS.append((K,'doors',f'no route for {path}',f'    "{path}",\n',f'    "/never{path}",\n'))
for run,label,old,new in [
    ('doors','the old goal itself is no route','    "/agent-goal",\n','    "/never/agent-goal",\n'),
    ('doors','an action under the door is enough','REGISTERED.contains(&path)','REGISTERED.contains(&path) || path.starts_with("/oracle/")'),
    ('doors','a slash at the end is the same action','REGISTERED.contains(&path)','REGISTERED.contains(&path.trim_end_matches(\'/\'))'),
    ('doors','letters of any case','REGISTERED.contains(&path)','REGISTERED.iter().any(|known| known.eq_ignore_ascii_case(path))'),
]:
    PLANTS.append((K,run,label,old,new))
for label,old,new in [
    ('every action not built','if clarp_kernel::doors::registered(uri.path()) {','if true {'),
    ('every action no route','if clarp_kernel::doors::registered(uri.path()) {','if false {'),
    ('the %XX read before the list','if clarp_kernel::doors::registered(uri.path()) {','if clarp_kernel::doors::registered(&uri.path().replace("%69", "i")) {'),
    ('no route in JSON','ApiError::plain(StatusCode::NOT_FOUND, "not found")\n    }\n}','ApiError::new(StatusCode::NOT_FOUND, "not found")\n    }\n}'),
    ('the Oracle not asked','not_built_at(&uri, "The Oracle is")','not_built("The Oracle is")'),
    ('the logins not asked','not_built_at(&uri, "Signing a tool in or out from an app is")','not_built("Signing a tool in or out from an app is")'),
    ('the harness not asked','not_built_at(&uri, "Signing a harness in to a provider of models is")','not_built("Signing a harness in to a provider of models is")'),
    ('the old goal not asked','not_built_at(&uri, "A goal of this kind is")','not_built("A goal of this kind is")'),
]:
    PLANTS.append((S,'wire',label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
