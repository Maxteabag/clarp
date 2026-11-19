#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The Quota keeper (the janitor job quota-monitor): its rule in the kernel (which windows count, the name a window is
remembered by, when the owner is told, the receipt and the run's line, the lane's cadence), held to the Python Host's
own functions and stories; and its lane in the Host (the janitors' runner, the records it keeps beside the book, the
book's tables kept apart, the push, a Host beside another).
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--test', 'quota_keeper_vectors'],
    'host': ['test', '--test', 'host', '--', 'the_quota_keeper_tells_the_owner_once_where_a_window_runs_low'],
}
Q = 'clarp-kernel/src/janitors/quota.rs'
K = 'clarp-host/src/quota_keeper.rs'
B = 'clarp-host/src/janitors.rs'
# NOT PLANTED, each with its reason:
#   janitors.rs (kernel), that the Quota keeper runs here: planted with every other job of RUN_HERE in
#     `truth-findings.py`, with the words of a janitor that cannot run.
#   quota.rs `ran`: that a run that only asked for a recovery worked. No story of the Python Host asks for one without
#     telling the owner in the same look, and the Host's test has no recovery to ask.
#   quota_keeper.rs `switcher_tells` and the generation guard of `deliver`: the kernel's stories hold the rules they
#     stand on (SWITCHED, SET_ANEW, plants below); a Host test of them needs an automatic switcher, which runs only on a
#     Host that keeps the logins with the owner's seats tool, or a push that fails, which the pusher never does.
#   quota_keeper.rs, that a receipt kept already is not made again: a crossing needs a newer reading than the one
#     kept, so the rule never makes one id twice; the guard is the Python Host's insert-or-ignore.
#   quota_keeper.rs, the moment a look is due (`if now < next`): a wrong one looks again two seconds later, which a
#     test sees only by waiting on a clock it cannot move.
PLANTS = [
    # ---- the name a window is remembered by
    (Q, 'kernel', "Claude's moment of a new window is not rounded to the nearest minute", '            let minute = floor_div(seconds(at) + 30.0, 60.0) as i64;', '            let minute = floor_div(seconds(at), 60.0) as i64;'),
    (Q, 'kernel', "every tool's windows are told apart as Claude's", '    if provider != "claude" {', '    if provider == "openai" {'),
    (Q, 'kernel', 'a moment without its offset is rounded too', '        Some((at, true)) => {', '        Some((at, _)) => {'),
    (Q, 'kernel', 'a digest is taken of the text as it is, not in ASCII', '    crate::live_call::sum(python::sorted_ascii(value).as_bytes())', '    crate::live_call::sum(python::canonical(value).as_bytes())'),
    (Q, 'kernel', "the old name's memory is carried over what the new name keeps", '        && memory.get(&key).is_none()\n', ''),
    # ---- when the owner is told
    (Q, 'kernel', 'a reading as old as the one kept counts', '        && prior["stamp"].as_i64().is_some_and(|kept| stamp <= kept)', '        && prior["stamp"].as_i64().is_some_and(|kept| stamp < kept)'),
    (Q, 'kernel', 'more than all used, or less than nothing, is a reading', '    if !(0.0..=100.0).contains(&left) {', '    if left.is_nan() {'),
    (Q, 'kernel', 'the threshold itself is not under it', '    let cross = (left <= threshold && (prior.is_none() || above(threshold)))', '    let cross = (left < threshold && (prior.is_none() || above(threshold)))'),
    (Q, 'kernel', 'a first reading under the threshold is not told', '    let cross = (left <= threshold && (prior.is_none() || above(threshold)))', '    let cross = (left <= threshold && above(threshold))'),
    (Q, 'kernel', 'nothing left is not told once more', '        || (left == 0.0 && prior.is_some() && above(0.0));', '        || false;'),
    (Q, 'kernel', 'what is left of a whole number used is a fraction', '        return Some(json!(100 - i128::from(whole)));', '        return Some(json!(100.0 - whole as f64));'),
    (Q, 'kernel', "a number is written with Python's g and its zeros", "            text.trim_end_matches('0').trim_end_matches('.').to_string()", '            text.to_string()'),
    (Q, 'kernel', 'a window that is not fresh counts', '            if window["freshness"] != "fresh" || !used.is_number() {', '            if !used.is_number() {'),
    (Q, 'kernel', 'a tool that names no account is of no account', '                None => json!(provider_id),', '                None => json!(""),'),
    (Q, 'kernel', 'an empty label is shown', '            let label = window["label"].as_str().filter(|label| !label.is_empty());', '            let label = window["label"].as_str();'),
    (Q, 'kernel', "a receipt is the same in every generation of its keeper", '                stamp,\n                owner.generation\n', '                stamp,\n'),
    (Q, 'kernel', 'an automatic switcher tells of Codex too, and the keeper does', 'pub const SWITCHED: [&str; 2] = ["claude", "codex"];', 'pub const SWITCHED: [&str; 2] = ["claude", "claude"];'),
    # ---- the run and the recovery
    (Q, 'kernel', 'a selector with no runtime is no selector', '        Accounts::Select => json!({ "status": "runtime_unavailable" }),', '        Accounts::Select => json!({ "status": "unconfigured" }),'),
    (Q, 'kernel', "the recoveries' words are run together", '    let reason = python::first(&recoveries.join("; "), REASON_MOST).to_string();', '    let reason = python::first(&recoveries.join(""), REASON_MOST).to_string();'),
    # ---- the lane
    (Q, 'kernel', 'a keeper set anew forgets when the last look was', '            last_check: kept.last_check,', '            last_check: None,'),
    (Q, 'kernel', 'a look is not counted as the last', '        self.last_check = Some(now_ms);', ''),
    (Q, 'kernel', 'the interval grows to a day where the keeper keeps it fixed', '        let bounds = (base, Some(longest));', '        let bounds = (base, None);'),
    # ---- the Host
    (B, 'host', 'the runner does not look at the keeper', '    crate::quota_keeper::look(app);', ''),
    (B, 'host', 'a change of a janitor leaves the keeper armed', '        app.quota_keeper.disarm();', ''),
    (B, 'host', "the book's tables are kept in its one record too", '                .and_then(|fields| fields.remove(table))', '                .and_then(|fields| fields.get(table).cloned())'),
    (B, 'host', 'a book kept whole stays whole', '            if kept_whole {', '            if false {'),
    (K, 'host', 'a Host beside another pushes too', '        } else if app.config.accounts == Accounts::Follow {', '        } else if false {'),
    (K, 'host', 'what became of a push is not written', '        receipt.delivery = Some(delivery);', '        let _ = delivery;'),
    (K, 'host', 'a receipt settled long ago is kept for ever', '                .is_some_and(|at| at + SETTLED_KEPT_MS <= now)', '                .is_some_and(|_| false)'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
