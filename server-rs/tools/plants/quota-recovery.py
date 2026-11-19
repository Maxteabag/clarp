#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The Quota keeper's recovery under `--accounts select` (piece 3): how a recovery is asked at a window with nothing
left (a card where the keeper asks, the tool's pool where it recovers by itself), what the runtime checks before it
asks the pool, the cards the owner accepted and which were acted on, and the pool's first turn it still owns; held to
the Python Host's request_recovery, recover_approved and runtime_recover (quota-keeper-vectors.py) and through the Host.
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--test', 'quota_keeper_vectors'],
    'host': ['test', '--test', 'host', '--',
             'the_quota_keeper_recovers_through_the_owners_selector_at_once_or_when_the_owner_says_so'],
}
Q = 'clarp-kernel/src/janitors/quota.rs'
A = 'clarp-kernel/src/accounts.rs'
K = 'clarp-host/src/quota_keeper.rs'
L = 'clarp-host/src/library.rs'
# NOT PLANTED, each with its reason:
#   quota.rs `approved`, that a card of another generation is passed over: the runtime's own check (`may_recover`,
#     the card's generation) answers approval_required for it all the same, and nothing is recovered either way,
#     as on the Python Host, whose recover_approved and runtime_recover both check it.
PLANTS = [
    # ---- how a recovery is asked
    (Q, 'kernel', 'a tool without the owner\'s selector is recovered all the same', '    if !selector {\n        return Some(Asked::Unconfigured);\n    }\n', ''),
    (Q, 'kernel', 'a keeper that asks recovers by itself', '    Some(if mode == "ask" {\n        Asked::Card', '    Some(if mode == "never" {\n        Asked::Card'),
    (Q, 'kernel', "the card's title is the tool's name as it is written", '        "title": format!("{titled} quota exhausted"),', '        "title": format!("{provider} quota exhausted"),'),
    (Q, 'kernel', 'the card names no receipt', '        "reference_id": receipt_id,', '        "reference_id": "",'),
    # ---- what the runtime checks
    (Q, 'kernel', 'a keeper set anew recovers for the old setting',
     '        standing.filter(|now| now.enabled && now.job == ROLE && now.generation == generation)',
     '        standing.filter(|now| now.enabled && now.job == ROLE)'),
    (Q, 'kernel', 'a card turned down approves', '        let accepted = card.is_some_and(|card| card["decision"]["resolved_choice"] == "accepted");',
     '        let accepted = card.is_some();'),
    (Q, 'kernel', 'a keeper that only tells may recover', '    } else if now.mode != "automatic" {', '    } else if now.mode == "never" {'),
    # ---- the cards accepted, and acted on once
    (Q, 'kernel', 'a card acted on is acted on again', '            (ours && !acted(id)).then_some', '            ours.then_some'),
    (Q, 'host', 'a card is done with only when a recovery is under way', '    status == "recovering" || status == "nothing_to_resume"', '    status == "recovering"'),
    (Q, 'kernel', 'a pool that did not take the request is recovering', '        Some(false) => "waiting",', '        Some(false) => "recovering",'),
    # ---- the pool and the Host
    (A, 'host', "the pool's first turn is one its agent no longer owns", '        let first = self.attempts.iter().find(|attempt| attempt.owned)?;',
     '        let first = self.attempts.iter().find(|attempt| !attempt.owned)?;'),
    (K, 'host', "a Host that keeps the logins reads no owner's answer", '    let selects = app.config.accounts == Accounts::Select;', '    let selects = false;'),
    # ---- the keeper is quiet: its shelf is opened by its name or by the exact id of a card on it (the Label
    #      checker's Fleet::scoped and this piece's Fleet::holding), never for a list the roster's members make
    (L, 'host', "the keeper reads its owner's answers from no shelf", '    let fleet = Fleet::scoped(app, session, "");',
     '    let fleet = Fleet::of(app);'),
    (L, 'host', "a janitor's card is among what claims the owner's attention",
     '        let agents = directory.members.iter();', '        let agents = directory.members.iter().chain(&directory.janitors);'),
    (L, 'host', "a janitor's card pages the owner", '    if muted || quiet {', '    if muted || (quiet && false) {'),
    (L, 'host', "a janitor's card cannot be answered by its id", '    let fleet = Fleet::holding(app, &decision_id);',
     '    let fleet = Fleet::of(app);'),
    (L, 'host', "a janitor's session lists none of its cards",
     '        let fleet = Fleet::scoped(&app, &listing.session, &listing.agent_id);', '        let fleet = Fleet::of(&app);'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
