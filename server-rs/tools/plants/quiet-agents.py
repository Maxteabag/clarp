#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Quiet agents: one whose activity never claims its owner's attention by itself is quiet, today exactly the janitors
(the kernel's one definition, `roster::quiet`, held by the Python Host's `quiet_agents.is_quiet`). It is asked where a
record of either kind can appear: the snapshot's row and what a turn tells (`wire.rs`), and the Live Activity
(`chip.rs`), whose stream of turns tells of a janitor's turns too. Where only the roster's members are walked (the
attention list, the Updates and the badge in `library.rs`, a request's push through `Directory::by_session`), the
separation of members and janitors is the rule: a janitor which asks, publishes and ends a turn reaches none of them. Held by the Host's test of an agent made a janitor while its turn
runs. Whether a turn a person began can end as a janitor's: not here (an agent at work or with work waiting is not
made a janitor, and a janitor's chat takes no message); the rule of what a quiet agent's turn tells is held by the
notification's own test.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'quiet_agents_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_janitor_reaches_none_of_its_owners_attention'],
    'told': ['test', '-p', 'clarp-host', '--lib', 'wire::tests::a_finished_turn_is_told_to_the_user'],
}
K = 'clarp-kernel/src/roster.rs'
W = 'clarp-host/src/wire.rs'
L = 'clarp-host/src/library.rs'
C = 'clarp-host/src/chip.rs'
F = 'clarp-host/src/fleet.rs'
BY_SESSION = '''        self.members
            .iter()
            .find(|member| member.record.session == session)'''
PLANTS = [
    (K, 'vectors', 'no agent is quiet', 'pub fn quiet(is_janitor: bool) -> bool {\n    is_janitor\n}', 'pub fn quiet(is_janitor: bool) -> bool {\n    let _ = is_janitor;\n    false\n}'),
    (K, 'told', 'every agent is quiet', 'pub fn quiet(is_janitor: bool) -> bool {\n    is_janitor\n}', 'pub fn quiet(is_janitor: bool) -> bool {\n    let _ = is_janitor;\n    true\n}'),
    (W, 'told', "a turn tells as if its agent were not quiet", '        quiet: record.is_quiet(),\n', ''),
    (W, 'host', "the snapshot's row says a janitor is not quiet", '"quiet": record.is_quiet(),', '"quiet": record.is_helper(),'),
    (L, 'host', "the roster's walk takes the janitors too (attention, Updates, badge)", '        let agents = directory.members.iter();', '        let agents = directory.members.iter().chain(directory.janitors.iter());'),
    (C, 'host', 'the lock screen is not told which agents are quiet', '            janitor: record.is_quiet(),', '            janitor: false,'),
    (F, 'host', 'an agent asked for by its session may be a janitor (a request, a document, a push)', BY_SESSION, '''        self.members
            .iter()
            .chain(self.janitors.iter())
            .find(|member| member.record.session == session)'''),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
