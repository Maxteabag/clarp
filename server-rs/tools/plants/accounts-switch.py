#!/usr/bin/env python3
"""Falsify reading, candidate selection, confirmation, admission and projection."""
import runner

RUNS = {"rule":["test","--test","accounts_switch_vectors"],"desk":["test","--test","host","--","the_desk_and_automatic_switcher_move_only_an_owned_idle_login"]}
K="clarp-kernel/src/seat_switch.rs"
M="clarp-host/src/seats/switching.rs"
A="clarp-host/src/seats/automatic.rs"
PLANTS=[]


def rules(file, owner, entries):
    PLANTS.extend((file,owner,reason,before,after) for before,after,reason in entries)


rules(K,"rule",[
    ("100.0 - used.as_f64()? * 100.0","used.as_f64()? * 100.0","remaining quota is used quota"),
    (".round_ties_even()",".round()","round a tie away from zero"),
    (".clamp(0.0, 100.0)",".clamp(-1.0, 100.0)","allow negative remaining quota"),
    (".clamp(0.0, 100.0)",".clamp(0.0, 101.0)","allow quota over one hundred"),
    ('["Fable", "Opus", "Sonnet", "Haiku"]','["Fable", "Missing", "Sonnet", "Haiku"]',"drop the Opus family"),
    ('if lower.contains(&family)','if lower == family',"refuse a model-qualified family"),
    ('Some((lower, model.into()))','Some((model.into(), model.into()))',"retain capitals in a custom family key"),
    ('b"._ -".contains(&b)','b"._ /-".contains(&b)',"admit a slash into a family key"),
    ('.filter(|s| truthy(&s["alias"]))','.filter(|s| s.is_object())',"read an account without an alias"),
    ('active: truthy(&row["is_active"])','active: row["is_active"] == true',"discard numeric active flags"),
    ('if tool == "codex"','if tool == "claude"',"use the other provider reader"),
    ('if !truthy(&usage["error"])','if usage["error"].is_null() || true',"accept failed Codex quota"),
    ('if truthy(&usage["blocked"])','if !truthy(&usage["blocked"])',"reverse a Codex block"),
    ('usage["usable"] == false','usage["usable"] == true',"reverse usable status"),
    ('percent(&usage["worst_used"])','percent(&usage["used"])',"read the wrong Codex window"),
    ('if !truthy(&row["error"])','if truthy(&row["error"])',"trust only failed Claude readings"),
    ('truthy(&usage["extra_usage_enabled"])','truthy(&usage["extra_usage_disabled"])',"ignore authorized overage"),
    ('Some("allowed" | "allowed_warning")','Some("allowed_warning")',"ignore allowed overage status"),
    ('Some("allowed" | "allowed_warning")','Some("allowed")',"ignore warning overage status"),
    ('!seat.overage\n                    &&','seat.overage\n                    &&',"reject the week despite overage"),
    ('usage["status_7d"] == "rejected"','usage["status_7d"] == "blocked"',"ignore weekly rejection"),
    ('truthy(&usage["limited"])','truthy(&usage["rate_limited"])',"ignore an explicit limit"),
    ('usage["status"] == "rejected"','usage["status"] == "error"',"ignore general rejection"),
    ('usage["available"] == false','usage["available"] == true',"invert availability"),
    ('seat.degraded = truthy(&usage["degraded"]);','seat.degraded = false;',"assume detailed evidence exists"),
    ('.take(32)\n                {','.take(31)\n                {',"drop the 32nd blocked entry"),
    ('.take(32)\n                {','.take(33)\n                {',"take a 33rd blocked entry"),
    ('flatten().take(32) {','flatten().take(31) {',"drop the 32nd scoped entry"),
    ('flatten().take(32) {','flatten().take(33) {',"take a 33rd scoped entry"),
    ('if scoped["exhausted"] == true','if scoped["exhausted"] == false',"invert scoped exhaustion"),
    ('*n = (*n).min(left);','*n = (*n).max(left);',"take the loosest duplicate scoped window"),
    ('if left == 0 && !seat.blocked_models.contains','if left > 0 && !seat.blocked_models.contains',"block models with room"),
    ('if self.degraded {','if !self.degraded {',"reverse degraded model evidence"),
    ('left >= rung.max(1)','left > rung.max(1)',"reject the exact rung"),
    ('left >= rung.max(1)','left >= rung.max(0)',"take an exhausted account at rung zero"),
    ('Some(ask.floor),','Some(100),',"ignore the owner floor"),
    ('(10 < ask.floor).then_some(10)','(10 < ask.floor).then_some(9)',"lower the middle rung to nine"),
    ('(0 < ask.floor).then_some(0)','(0 < ask.floor).then_some(1)',"raise the last rung to one"),
    ('.find(|s| s.active)', '.find(|s| !s.active)',"select a spare as the active seat"),
    ('Hotseat reports no account in use','Hotseat reading unavailable',"misreport a signed-out machine"),
    ('{}: quota unknown','{}: quota exhausted',"mistake unknown quota for exhaustion"),
    ('(0..ask.cooldown).contains(&age)','(0..=ask.cooldown).contains(&age)',"extend reversal cooldown by a millisecond"),
    ('(0..ask.cooldown).contains(&age)','(1..ask.cooldown).contains(&age)',"lift reversal protection during the switch instant"),
    (' && have > 0;', ' && have >= 0;', "keep cooldown after hard exhaustion"),
    ('|| regained','|| !regained',"reverse the model recovery exception"),
    ('s.secondary.is_some_and(|v| v <= 0) && !s.overage','s.secondary.is_some_and(|v| v <= 0) && s.overage',"strand a usable or exhausted weekly quota"),
    ('if clears(have, rung)','if !clears(have, rung)',"switch while the active seat clears the rung"),
    ('coverage(s) == req.len()\n                    }','coverage(s) == 0\n                    }',"prefer a seat serving no requested models"),
    ('values.sort_unstable();','values.sort_unstable_by(|a,b|b.cmp(a));',"rank strongest model ahead of weakest"),
    ('let running = specs(ask.running);','let running = specs(&[]);',"ignore running model demand"),
    ('let req = specs(ask.required);','let req = specs(&[]);',"ignore parked model demand"),
    ('let Some(left) = active.room(family) else {','let Some(left) = active.room(family).or(Some(0)) else {',"treat missing scoped evidence as zero"),
    ('if !safe && left <= 0','if !safe && left > 0',"reverse the test for a stranded model"),
    ('"blocked_model":label','"blocked_family":label',"lose the blocked model in a no-op"),
    ('"target_general_remaining":s.remaining','"target_general_remaining":s.room(family)',"report model quota as general quota"),
    ('"general_remaining":have','"general_remaining":left',"drop usable general quota on model exhaustion"),
    ('"would switch to"','"switched to"',"announce a switch in notify mode"),
    ('"could not switch to"','"might switch to"',"hide a switch failure"),
    ('no saved account has more','no saved seat',"change the exhaustion notice"),
    ('"5-hour"','"hourly"',"mislabel the Claude window"),
    ('"weekly"','"daily"',"mislabel the Codex window"),
])
rules(K, "rule", [
    ('weekly_used: None,', 'weekly_used: Some(None),', 'add the Codex field to Claude accounts'),
    ('seat.weekly_used = Some(None);', 'seat.weekly_used = None;', 'omit the failed Codex reading instead of giving null'),
    ('seat.weekly_used = Some(codex_weekly_used(usage));',
     'seat.weekly_used = Some(usage["worst_used"].as_number().cloned());',
     'substitute the switchers worst window for the account week'),
    ('kind == "7d"', 'kind == "5h"', 'read the five-hour window'),
    ('kind == "7d"', 'kind == "7d" || kind.starts_with("model:")', 'take model windows as account-wide'),
    ('let used = window["used"].as_number()?;',
     'let Some(used) = window["used"].as_number() else { continue; };',
     'ignore an unreadable duplicate account window'),
    ('used.as_f64() > best.as_f64()', 'used.as_f64() < best.as_f64()', 'take the smallest duplicate number'),
    ('used.as_f64() > best.as_f64()', 'used.as_f64() >= best.as_f64()', 'replace the first number on a tie'),
    ('const WEEKLY_SPENT: f64 = 0.99;', 'const WEEKLY_SPENT: f64 = 0.98;', 'lower the weekly threshold'),
    ('used >= WEEKLY_SPENT', 'used > WEEKLY_SPENT', 'refuse an account exactly at the weekly threshold'),
    ('&& !accounts.is_empty()', '', 'take an empty roster as eligibility evidence'),
    ('accounts.iter().all(|seat|', 'accounts.iter().any(|seat|', 'accept when only one saved account is spent'),
    ('.is_some_and(|used| used >= WEEKLY_SPENT)', '.is_none_or(|used| used >= WEEKLY_SPENT)',
     'count missing or failed weekly evidence as spent'),
    ('spent.is_some_and(|spent| spent < cap)', 'spent.is_none_or(|spent| spent < cap)',
     'permit a reset with an unreadable redemption count'),
    ('spent.is_some_and(|spent| spent < cap)', 'spent.is_some_and(|spent| spent <= cap)',
     'permit a reset when the daily cap has already been reached'),
    ('Option::deserialize(deserializer).map(Some)', 'Option::deserialize(deserializer)',
     'lose explicit null when reading a saved Codex account'),
])
rules("clarp-kernel/src/seats.rs", "rule", [
    ('.split(crate::python::blank)', '.split(char::is_whitespace)', 'ignore a Python whitespace separator'),
])
rules(M,"desk",[
    ('&["codex", "--no-quota", "--json"]','&["codex", "--json"]',"probe quota when reading only the active login"),
    ('&["switch", target, "--yes", "--json"]','&["switch", target, "--json"]',"omit the Claude confirmation flag"),
    ('&["codex-account", "switch", target]','&["codex-account", "use", target]',"use the wrong Codex profile command"),
    ('"hotseat_unavailable"','"switch_failed"',"mislabel an unreadable login"),
    ('"state":"running"','"state":"checking"',"hide a pending switch"),
    ('"reason":"already_active"','"reason":"already_switched"',"mislabel unchanged login"),
    ('if from.as_deref() == Some(target)','if from.as_deref() != Some(target)',"invert unchanged-login detection"),
    ('truthy(&v["switched"])','truthy(&v["active"])',"ignore Claude confirmation"),
    ('"error":"switch_failed"','"error":"unavailable"',"mislabel an unconfirmed switch"),
    ('record["from"] = json!(from);','record["from"] = Value::Null;',"erase the former alias in desk history"),
    ('record["finished_at"] = json!(at);','record["finished_at"] = Value::Null;',"omit the completion time"),
    ('pool.available(|_| true);','pool.available(|_| false);',"leave waiting work behind after a switch"),
])
rules("clarp-host/src/seats.rs","desk",[
    ('if app.config.accounts == Accounts::Select {\n        return switching::manual','if app.config.accounts != Accounts::Select {\n        return switching::manual',"permit switching in follow and none modes"),
    ('"logins_kept_elsewhere"','"inactive"',"mislabel another Host's ownership"),
])
rules(A,"desk",[
    ('for provider in ["claude", "codex"]','for provider in ["claude"]',"never check Codex"),
    ('if mode == "automatic" && action == "switch"','if mode == "notify" && action == "switch"',"reverse mode authority"),
    ('book.claim_run(rid, world, at).then','(!book.claim_run(rid, world, at)).then',"run only a failed claim"),
    ('if progress.generation != generation','if progress.generation == generation',"reuse previous-generation cadence"),
    ('if resume::switch_wanted(tool, target).is_ok()', 'if resume::switch_wanted(tool, target).is_err()', "discard valid automatic targets"),
    ('switched = answer.1["switched"] == true;', 'switched = answer.1["changed"] == true;', "misread successful automatic result"),
    ('if read.is_ok()', 'if read.is_err()', "discard every successful scheduled report"),
])
rules("clarp-host/src/agent.rs","desk",[
    ('pool.guard_turn(&mut order);','let _ = (&pool, &mut order);',"launch a provider without holding its login lock"),
])
rules(M,"desk",[
    ('"reason":"switch_in_progress"','"reason":"no_accounts"',"mislabel a still-running switch"),
    ('"pending":true','"pending":false',"claim an unfinished switch is over"),
    ('202,','200,',"return completed status to a request whose work is still running"),
])
# Each check has three independent counterexamples. Admit one extra mode at a
# time so a failure in follow cannot hide an untested path in none.
for effect, result in [("report the automatic switcher enabled","return false;"),
                       ("start automatic checks","return;")]:
    mode = "app.config.accounts != Accounts::Select"
    guard = f"if {mode} || !app.seats.told() {{\n        {result}"
    for extra, other in [("follow","None"),("none","Follow")]:
        PLANTS.append((A,"desk",f"{effect} under {extra}",guard,guard.replace(mode,f"app.config.accounts == Accounts::{other}")))
    PLANTS.append((A,"desk",f"{effect} without a seats tool",guard,guard.replace(" || !app.seats.told()", "")))

qual = "clarp_kernel::recovery::Accounts::"
select = f"app.config.accounts == {qual}Select"
# Where the features are decided, the desk's own reading (`seats`: a tool told, and the desk
# not turned off by the owner) stands beside the account mode since differences-truth part 2;
# without a seats tool is the mode alone with the owner's switch.
for file, effect, test, toolless in [
        ("clarp-host/src/edge.rs","advertise seat actions", select+" && seats", select+" && !crate::seats::turned_off(app)"),
        # One line since truth-findings commit 3, where the seats a Host keeps are read once.
        ("clarp-host/src/janitors.rs","allow enabling a Hotseat Janitor",select+" && app.seats.told()",select)]:
    for extra, other in [("follow","None"),("none","Follow")]:
        PLANTS.append((file,"desk",f"{effect} under {extra}",test,test.replace(select,f"app.config.accounts != {qual}{other}")))
    PLANTS.append((file,"desk",f"{effect} without a seats tool",test,toolless))

guard = "if app.config.accounts == Accounts::Select {\n        return switching::manual"
for extra, other in [("follow","None"),("none","Follow")]:
    PLANTS.append(("clarp-host/src/seats.rs","desk",f"admit a manual switch under {extra}",guard,
                   guard.replace("== Accounts::Select",f"!= Accounts::{other}")))
rules(M,"desk",[
    ("if !app.seats.told() {","if app.seats.told() && tool.is_empty() {","enter a manual switch without a configured seats tool"),
])
# All mutation runs use synthetic programs and an isolated Host directory.
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
