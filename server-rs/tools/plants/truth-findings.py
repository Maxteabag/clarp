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
J='clarp-kernel/src/janitors.rs'
H='clarp-host/src/janitors.rs'
RUNS['words']=['test','-p','clarp-kernel','--lib','a_janitor_that_cannot_run_is_told']
RUNS['auditor']=['test','--test','host','a_janitors_scheduled_task_runs_when_its_schedule_says']
RUNS['seats']=['test','--test','host','the_desk_and_automatic_switcher_move_only_an_owned_idle_login']
for job in ['runs::SCHEDULED_TASK,','crate::router::JOB,','"tool-explainer",','crate::goals::keeper::ROLE,','audio::ROLE,','quota::ROLE,\n]']:
    PLANTS.append((J,'words',f'{job.split(",")[0]} does not run','    '+job.replace('\\n','\n'),'    "never",'+('\n]' if job.endswith(']') else '')))
for file,run,label,old,new in [
    (J,'words','the switcher of seats runs everywhere','(seats && job == SEATS_JOB)','job == SEATS_JOB'),
    (J,'words','every job runs with the seats','(seats && job == SEATS_JOB)','seats'),
    (J,'words','the jobs said without their last','Some((last, before)) => format!("{} or {last}", before.join(", ")),','Some((_, before)) => before.join(", "),'),
    (J,'words','every job said','.filter(|template| !runs_here(template["id"].as_str().unwrap_or_default(), seats))','.filter(|_| true)'),
    (J,'words','where the switcher runs not said','    if !seats {\n        said.push_str(','    if false {\n        said.push_str('),
    (J,'words','where the switcher runs said always','    if !seats {\n        said.push_str(','    if true {\n        said.push_str('),
    # Not here: a seats tool not asked for is accounts-switch's 'allow enabling a Hotseat Janitor
    # without a seats tool', the same rule in the same place, and that list came first.
    (H,'auditor','the words of a Host with the seats','let why = rules::cannot_run(seats);','let why = rules::cannot_run(true);'),
    (H,'seats','the switcher let on everywhere','if on && !rules::runs_here(job, seats) {','if on && !rules::runs_here(job, true) {'),
    (H,'auditor','every janitor turned on','if on && !rules::runs_here(job, seats) {','if false {'),
]:
    PLANTS.append((file,run,label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
