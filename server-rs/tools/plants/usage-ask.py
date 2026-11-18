#!/usr/bin/env python3
"""Refresh age, projection, explicit refresh and the fallback, each independently falsified."""
import runner

K = "clarp-kernel/src/usage_ask.rs"
P = "clarp-host/src/usage_provider.rs"
RUNS = {
    "vectors": ["test", "--test", "usage_ask_vectors"],
    "reader": ["test","--lib","provider_readers_agree_with_the_isolated_reference"],
    "host": ["test", "--test", "host", "--", "asking_for_usage_reads_each_provider"],
}
# Do not mutate the destination, credential boundary or opt-in default into a real provider call.
PLANTS = [
    (K, "vectors", "refresh at the five-minute boundary", "> 300_000", ">= 300_000"),
    (K, "vectors", "discard freshness on the hour", "<= crate::standing::FRESH_MS", "< crate::standing::FRESH_MS"),
    (K, "vectors", "the reset instant is still fresh", "now >= end", "now > end"),
    (K, "vectors", "weekly buckets have five hours", '("seven_day", Some(10080))', '("seven_day", Some(300))'),
    (K, "vectors", "do not count remaining fractions", "(1.0 - fraction) * 100.0", "fraction * 100.0"),
    (P, "host", "skip Codex fallback", 'direct.is_some() || tool != "codex"', 'direct.is_some() || tool == "codex"'),
]

# Change the projection in isolation. The comparison includes the stable identifier and
# every field of both the per-window and per-provider shapes.
for before, after, reason in [
    ('pointer("/command/data/groups")','pointer("/command/groups")','read groups from the wrong object'),
    ('if groups.is_empty()', 'if groups.len() == 1','discard the one-group report'),
    ('Value::Bool(on) => f64::from(*on as u8)', 'Value::Bool(on) => f64::from(!*on as u8)','invert a boolean quota fraction'),
    ('Some(n) => n,','Some(n) => n.clamp(0.0,1.0),','clamp out-of-range readings'),
    ('None => continue,','None => 0.0,','count a nonnumeric fraction as spent'),
    ('("{:.1}",','("{:.0}",','round the percentage to a whole number'),
    ('("five_hour", Some(300))','("five_hour", Some(60))','use a one-hour window ID'),
    ('("unknown", None)','("unknown", Some(0))','unknown durations count as zero minutes'),
    ('"puw-",','"pu-",','change window ID prefix'),
    ('"schema_version":1','"schema_version":2','change ID schema'),
    ('"auth_generation_id":"agy-usage-command"','"auth_generation_id":"agy"','hash a different source'),
    ('.unwrap_or(label)', '.unwrap_or("")', 'drop group-name fallback for bucket identity'),
    ('"scope_kind":"account"','"scope_kind":"model_group"','derive window ID from displayed scope'),
    ('"window_minutes":minutes','"window_minutes":null','drop duration from stable ID'),
    ('v>0.0','v>=0.0','include sliding resets of unused buckets'),
    ('"provider_reset_unknown"','"unknown"','change marker for unknown reset'),
    ('                32,','                24,','truncate the stable identifier'),
    ('"window_id":id,','"id":id,','omit public window ID'),
    ('"scope":"model_group"','"scope":"account"','lose model-group scope'),
    ('"label":label,','"label":kind,','show window name instead of group'),
    ('"used_percentage":used,','"used_percentage":fraction,','show a fraction as percentage'),
    ('"resets_at":reset,','"resets_at":null,','omit the bucket reset'),
    ('"normal"} else {"unknown"','"normal"} else {"normal"','keep stale bucket normal'),
    ('"episode_id":null','"episode_id":""','invent a limit episode'),
    ('"provider_limit_event_id":null','"provider_limit_event_id":""','invent an event reference'),
    ('"provider_id":"agy"','"provider_id":"antigravity"','change provider wire name'),
    ('any.then(||moment(at))','any.then(||moment(now))','date observation to the request'),
    ('"detail":NOTHING_SEEN','"detail":""','erase unavailable evidence explanation'),
    ('"kind":"unavailable"','"kind":"unknown"','misclassify absent evidence'),
    ('"detail":"agy-usage-command"','"detail":"agy"','mislabel command evidence'),
    ('if any {age} else {"unknown"}','if any {age} else {"fresh"}','show absent observations as fresh'),
]:
    PLANTS.append((K, "vectors", reason, before, after))

B = "clarp-backend/src/limits.rs"
for before,after,reason in [
    ('if clarp_kernel::python::truthy(apps)', 'if !apps.is_null()', 'prefer an empty OAuth week'),
    ('("five_hour", 300, &payload["five_hour"])','("five_hour", 301, &payload["five_hour"])','wrong Claude session length'),
    ('("seven_day", 10080, weekly)','("seven_day", 10081, weekly)','wrong Claude week length'),
    ('"used_percentage":used,"window_minutes":minutes','"used_percentage":used*100.0,"window_minutes":minutes','scale OAuth percentages twice'),
    ('time(row.get("resets_at"))','time(row.get("resetsAt"))','lose Claude reset time'),
    ('(!out.is_empty()).then_some(Value::Object(out))','Some(Value::Object(out))','accept empty responses'),
]:
    PLANTS.append((B,"reader",reason,before,after))

for before,after,reason in [
    ('limits["allowed"] == false','limits["allowed"] == true','invert provider refusal'),
    ('limits["limit_reached"] == true','limits["limit_reached"] == false','invert limit-reached field'),
    ('.contains("credit")','.contains("quota")','lose workspace credit exhaustion'),
    ('"/rate_limit_upsell/reset_at"','"/rate_limit/reset_at"','read wrong credit reset'),
    ('"credits_depleted"} else {"usage_limit"','"usage_limit"} else {"credits_depleted"','swap reasons for a block'),
    ('"chatgpt.com" | "chat.openai.com"','"chatgpt.com"','drop the accepted alternate origin'),
    ('format!("https://{host}/backend-api/wham/usage")','format!("https://{host}/wham/usage")','change approved route'),
]:
    PLANTS.append((P,"reader",reason,before,after))

for before,after,reason in [
    ('.header("ChatGPT-Account-Id", account)', '.header("ChatGPT-Account", account)', 'lose the account selection header'),
    ('.header("User-Agent", "codex-cli")','.header("User-Agent", "clarp")','change Codex client header'),
    ('"oauth-2025-04-20"','"oauth-2025-04-21"','change the OAuth version'),
    ('"clarp-provider-usage"','"clarp"','change initializing client identity'),
    ('"version":"1"','"version":"2"','change initializing client version'),
    ('["-s", "read-only"]','["-s", "readonly"]','misspell sandbox flag'),
    ('["never", "on-request", ""]','["on-request","never",""]','change approval spelling order'),
    ('["--output-format", "json", "-p", "/usage"]','["--output-format", "json", "-p", "/status"]','ask agy the wrong command'),
    ('msg["id"] != id','msg["id"] == id','consume unrelated JSON-RPC messages'),
    ('source: "anthropic-oauth-usage"','source: "none"','mislabel Claude OAuth source'),
    ('codex_read(result, "account/rateLimits/read")','codex_read(result, "app-server")','mislabel app-server evidence'),
]:
    PLANTS.append((P,"host",reason,before,after))

Q = "clarp-kernel/src/quota.rs"
S = "clarp-kernel/src/standing.rs"
PLANTS.extend([
    (Q, "vectors", "forget a recovered episode while its window is absent", ".or(episode.omitted_window_end_ms)", ".or(None)"),
    (S, "vectors", "a changed Claude account never makes a reading due", "other || crate::usage_ask::due", "false && other || crate::usage_ask::due"),
    (Q, "vectors", "treat equal account identifiers as rotated", "on.account != Some(account.as_str())", "on.account == Some(account.as_str())"),
    (Q, "vectors", "ignore the last authoritative refresh", "crate::usage_ask::due(self.authoritative_ms, now)", "self.authoritative_ms.is_none()"),
    (Q, "vectors", "a completed turn before the observation invalidates it", "done > self.authoritative_ms.unwrap_or(0)", "done <= self.authoritative_ms.unwrap_or(0)"),
    (Q, "vectors", "ignore a completed turn exactly at the current instant", "unwrap_or(0) && done <= now", "unwrap_or(0) && done < now"),
    (Q, "vectors", "do not refresh a whole 100 percent reading", "n >= 100.0", "n > 100.0"),
    (Q, "vectors", "a failed refresh erases prior percentage", "self.row.as_ref().is_none_or(|r| r.used.is_null())", "self.row.as_ref().is_none_or(|r| !r.used.is_null())"),
    (Q, "vectors", "date refresh failure before it happens", "self.authoritative_ms = Some(now);", "self.authoritative_ms = Some(now-1000);"),
    (Q, "vectors", "erase error on failed collection", 'self.error = "Codex usage unavailable".into();', 'self.error = String::new();'),
    (Q, "vectors", "treat a complete reading as sparse", 'matches!(detail, "/wham/usage" | "account/rateLimits/read")','detail == "account/rateLimits/read"'),
    (Q, "vectors", "retain omitted windows in legacy row", ".filter(same).filter(|_| !whole)", ".filter(same)"),
    (Q, "vectors", "never take a direct block", 'self.block = snapshot.get("blocked").cloned();','self.block = None;'),
    (Q, "vectors", "leave an omitted session window active", '"five_hour" => five_hours.is_none()', '"five_hour" => false'),
    (Q, "vectors", "leave an omitted weekly window active", '"seven_day" => week.is_none()', '"seven_day" => false'),
    (Q, "vectors", "mark an omitted window as stale", 'freshness: "unknown",\n                        source_kind: "authoritative_window_omitted"', 'freshness: "stale",\n                        source_kind: "authoritative_window_omitted"'),
    (Q, "vectors", "wrong reason for an omitted limit event", 'source_kind: "authoritative_window_omitted"', 'source_kind: "provider_reported"'),
    (Q, "vectors", "retain retired windows", 'self.windows.retain(|w| !omitted(w));','self.windows.iter().for_each(|_| ());'),
    (Q, "vectors", "clear an unknown terminal even at 100 percent", 'unwrap_or(0.0) < 100.0','unwrap_or(0.0) <= 100.0'),
    (Q, "vectors", "retain unknown hard limits after usable reading", 'events.extend(self.clear_unknown(tool, Some(login), now_ms, new_id));','events.extend(Vec::<Value>::new());'),
    (Q, "vectors", "expire the workspace block on its last fresh millisecond", "now_ms - at <= WINDOW_FRESH_MS", "now_ms - at < WINDOW_FRESH_MS"),
    (Q, "vectors", "forget the error on a missing measurement", '"freshness":"unknown","error":self.error', '"freshness":"unknown","error":""'),
    (Q, "vectors", "make a failed positive reading appear error free", '"error": self.error,','"error": "",'),
    (Q, "vectors", "mislabel the source in the legacy response", 'self.authoritative_ms.is_some() {"codex-usage-endpoint"}', 'self.authoritative_ms.is_some() {"codex-app-server"}'),
])


PLANTS.extend([
    (P, "reader", "drop sibling Codex buckets from the app-server result", 'codex_read(result, "account/rateLimits/read")', 'codex_read(&json!({"rateLimits":result["rateLimits"]}), "account/rateLimits/read")'),
    (P, "reader", "accept an HTTPS-shaped result from the RPC method", 'if !clarp_kernel::python::truthy(&result["rateLimits"])', 'if result.get("rate_limit").is_none() && !clarp_kernel::python::truthy(&result["rateLimits"])'),
])

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
