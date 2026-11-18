#!/usr/bin/env python3
"""Mutations of the alert router and its delivery at the Host."""
import runner

RUNS = {
    'android': ['test', '--test', 'fcm_vectors'],
    'activity': ['test', '--test', 'direct_activity_vectors'],
    'vectors': ['test', '--test', 'push_direct_vectors'],
    'host': ['test', '--test', 'host', '--', 'an_alert_uses_the_hosts_key'],
}
K = 'clarp-kernel/src/alert_routes.rs'
H = 'clarp-host/src/pusher.rs'
PLANTS = [
    (K, 'vectors', 'direct mode still uses a phone grant', 'matches!(mode, "auto" | "relay")', 'matches!(mode, "auto" | "relay" | "direct")'),
    (K, 'vectors', 'a grant can be used without a relay', 'grants: relay && matches!', 'grants: matches!'),
    (K, 'vectors', 'relay mode falls back to a key', 'fallback: direct_allowed && key', 'fallback: key'),
    (K, 'vectors', 'a revoked grant is retained', 'grant_revoked = true;', 'grant_revoked = false;'),
    (K, 'vectors', 'another phone retries the failed credential relay', 'self.base = Some(Path::Direct);', 'self.base = Some(Path::Account);'),
    (K, 'vectors', 'a grant outage repeats for each phone', 'self.grant_down = Some(why.clone());', 'self.grant_down = None;'),
    (K, 'vectors', 'only a key or credential enables alerts', 'base.is_some() || (relay && relay_allowed)', 'base.is_some()'),
    (H, 'host', 'a permanent token rejection is temporary', 'if status == 410', 'if status == 411'),
    (K, 'vectors', 'off mode uses account', '!matches!(mode, "off" | "direct")', 'mode != "direct"'),
    (K, 'vectors', 'direct mode selects account first', '!matches!(mode, "off" | "direct")', 'mode != "off"'),
    (K, 'vectors', 'off mode signs alerts', '!matches!(mode, "off" | "relay")', 'mode != "relay"'),
    (K, 'vectors', 'relay mode with no account signs alerts', '!matches!(mode, "off" | "relay")', 'mode != "off"'),
    (K, 'vectors', 'an account reaches a missing relay', 'if relay_allowed && relay && account', 'if relay_allowed && account'),
    (K, 'vectors', 'no credential is needed to bind a token', 'if relay_allowed && relay && account', 'if relay_allowed && relay'),
    (K, 'vectors', 'a key overrides the credential in auto', 'if relay_allowed && relay && account', 'if relay_allowed && relay && account && !key'),
    (K, 'vectors', 'direct requires no key', 'else if direct_allowed && key', 'else if direct_allowed'),
    (K, 'vectors', 'an ungranted phone uses another grant', 'if granted && self.grants', 'if self.grants'),
    (K, 'vectors', 'a grant is sent through the account', 'send(Path::Grant)', 'send(Path::Account)'),
    (K, 'vectors', 'a grant outage uses the account', "break 'route if self.fallback {\n                            send(Path::Direct)", "break 'route if self.fallback {\n                            send(Path::Account)"),
    (K, 'vectors', 'an outage without fallback disables the phone', 'Outcome::Down(why)\n                        };', 'Outcome::GrantRevoked\n                        };'),
    (K, 'vectors', 'a revoked grant always disables its phone', 'if self.base.is_none()', 'if true'),
    (K, 'vectors', 'a revoked grant with no other way becomes an outage', "break 'route Outcome::GrantRevoked;", "break 'route Outcome::Down(String::new());"),
    (K, 'vectors', 'a refused grant is counted sent', "other => break 'route other", "_ => break 'route Outcome::Sent"),
    (K, 'vectors', 'no route counts as a success', 'None => Outcome::NoPath', 'None => Outcome::Sent'),
    (K, 'vectors', 'account outage is tried again for the next phone', 'self.account_down = Some(why.clone());', 'self.account_down = None;'),
    (K, 'vectors', 'even without a key an outage tries direct', 'fallback: direct_allowed && key', 'fallback: direct_allowed'),
    (K, 'vectors', 'account path sends by grant', 'send(Path::Account)', 'send(Path::Grant)'),
    (K, 'vectors', 'grant-only base sends direct', 'Some(Path::Direct)\n        } else {\n            None', 'Some(Path::Direct)\n        } else {\n            Some(Path::Direct)'),
    (H, 'host', 'direct request is a call', 'push_type: push.push_type,', 'push_type: "voip",'),
    (H, 'host', 'direct alert is low priority', 'priority: push.priority,', 'priority: "5",'),
    (H, 'host', 'direct alert expires immediately', 'expiration: None,', 'expiration: Some(0),'),
    (H, 'host', 'direct alert replaces another notification', 'collapse_id: push.collapse_id,', 'collapse_id: "another-alert",'),
    (H, 'host', 'direct request omits the alert', 'payload: push.payload,', 'payload: &serde_json::Value::Null,'),
    (H, 'host', 'direct request addresses another device', '(&row.token, &row.environment),', '("wrong-device", &row.environment),'),
    (H, 'host', 'a successful direct send is a refusal', 'Ok((200, _, _)) => Outcome::Sent', 'Ok((201, _, _)) => Outcome::Sent'),
    (H, 'host', 'only the HTTP status can retire a dead device', '|| clarp_kernel::direct_push::GONE_FOR_GOOD.contains(&reason)', '|| reason == "imaginary-token-error"'),
    (H, 'host', 'a refusal disables a live phone', 'Outcome::Refused {\n                                    status,\n                                    reason: reason.to_string(),\n                                }', 'Outcome::Dead(reason.to_string())'),
    (H, 'host', 'a revoked grant is left on the registration', 'if delivery.grant_revoked {', 'if false && delivery.grant_revoked {'),
    (H, 'host', 'relay account sends the wrong token', 'relay.to_token(&row.token, &row.environment, &push)', 'relay.to_token("wrong-device", &row.environment, &push)'),
    (H, 'host', 'relay grant sends the wrong bearer', 'relay.to_grant(&row.push_grant, &push)', 'relay.to_grant("wrong-grant", &push)'),
    ('clarp-host/src/direct.rs', 'host', 'mode is read from the wrong section', 'text_at(&file, &["apns", "mode"])', 'text_at(&file, &["other", "mode"])'),
    ('clarp-host/src/direct.rs', 'host', 'uppercase direct is treated as auto', '|mode| mode.to_lowercase()', '|mode| mode'),
]
F = 'clarp-kernel/src/fcm.rs'
for what, old, new in [
    ('null is sent as text', 'Value::Null => None', 'Value::Null => Some("null".into())'),
    ('whole float keeps decimal', 'Some(format!("{:.0}", n.as_f64()? + 0.0))', 'Some(python::canonical(value))'),
    ('nested maps keep insertion order', 'value => Some(python::canonical(value))', 'value => Some(python::compact(value))'),
    ('wrong payload version', 'json!("1")', 'json!("2")'),
    ('null alert value becomes a label', 'if !value.is_null() && value != ""', 'if value != ""'),
    ('empty alert value is sent', 'if !value.is_null() && value != ""', 'if !value.is_null()'),
    ('APS sent as a data field', 'if key != "aps"', 'if true'),
    ('context overwrites the payload', 'data.entry(key.clone()).or_insert(json!(text));', 'data.insert(key.clone(),json!(text));'),
    ('collapse id overrides context', 'data.entry("collapse_id").or_insert(json!(collapse));', 'data.insert("collapse_id".into(),json!(collapse));'),
    ('collapse id sent even when absent', 'if !collapse.is_empty()', 'if true'),
    ('time supplied by payload accepted', 'data.insert("sent_at".into(), json!(now_ms.to_string()));', 'data.entry("sent_at").or_insert(json!(now_ms.to_string()));'),
    ('reserved google prefix allowed', '!k.starts_with("google")', '!k.starts_with("google.")'),
    ('reserved gcm prefix allowed', '!k.starts_with("gcm")', '!k.starts_with("gcm.never.")'),
    ('the data fits one byte fewer', 'usize = 4096', 'usize = 4095'),
    ('the data fits one byte more', 'usize = 4096', 'usize = 4097'),
    ('reason can be cut on ordinary alerts', 'if key == "reason" && data["kind"] != "agent-ring"', 'if false'),
    ('ring reason cannot be shortened', 'if key == "reason" && data["kind"] != "agent-ring"', 'if key == "reason"'),
    ('escape expansion ignored', 'let bytes = json!(c.to_string()).to_string().len() - 2;', 'let bytes = c.len_utf8();'),
    ('omit truncation suffix', "prefix.push('…');", "prefix.push('.');"),
    ('reference timestamp shifted', 'json!(now_ms.to_string())', 'json!((now_ms+1).to_string())'),
    ('transport high priority wrong', 'if priority == "10"', 'if priority == "5"'),
    ('collapse group always present', 'if !collapse_key.is_empty()', 'if true'),
    ('activity needs active and waiting agents', '|| python::truthy(&state["needs_you"])', '&& python::truthy(&state["needs_you"])'),
    ('idle activity reported as update', '{"update"} else {"end"}', '{"update"} else {"update"}'),
    ('empty host id is sent', 'if !server.is_empty()', 'if true'),
    ('wrong accepted message name', 'body["name"].as_str()', 'body["id"].as_str()'),
    ('refusal never refreshes bearer', 'if status == 401 && !self.refreshed', 'if status == 402 && !self.refreshed'),
    ('refresh repeated indefinitely', 'self.refreshed = true;', 'self.refreshed = false;'),
    ('explicit token retirement ignored', 'code == "UNREGISTERED"', 'code == "RETIRED"'),
    ('all 404 errors retire the token', '(status == 404 && code.is_empty())', '(status == 404)'),
    ('incorrect status for retired token', '410, "Unregistered".into()', '404, "Unregistered".into()'),
    ('payload field attributed to token', 'if !other {', 'if true {'),
    ('wrong invalid registration reason', '410, "InvalidRegistration".into()', '410, "Invalid".into()'),
    ('invalid argument RPC status ignored', '|| rpc == "INVALID_ARGUMENT"', '|| rpc == "OTHER"'),
    ('invalid argument FCM code ignored', 'code == "INVALID_ARGUMENT"', 'code == "OTHER"'),
    ('first server failure never retried', 'status >= 500', 'status > 500'),
    ('last client failure retried', 'status >= 500', 'status >= 499'),
    ('network failure not retried', 'let transient = status == 0', 'let transient = status == 503'),
    ('rate limit not retried', '|| status == 429', '|| status == 430'),
    ('one fewer retry', 'self.transient < 2', 'self.transient < 1'),
    ('one extra retry', 'self.transient < 2', 'self.transient < 3'),
    ('backoff starts too high', 'let backoff = 1 <<', 'let backoff = 2 <<'),
    ('retry header wins over backoff', 'backoff.max(wait)', 'wait'),
    ('retry header capped too early', '.unwrap_or(u64::MAX).min(10)', '.unwrap_or(u64::MAX).min(9)'),
    ('retry header capped too late', '.unwrap_or(u64::MAX).min(10)', '.unwrap_or(u64::MAX).min(11)'),
    ('ring pushed at expiration', 'now >= expires', 'now > expires'),
    ('superseded ringing state delivered', 'later || now >= expires', 'now >= expires'),
    ('known ring closes on untried tokens', 'tried.contains(token)', '!tried.contains(token)'),
    ('unknown ring end dropped', 'None => rows,', 'None => Vec::new(),'),
    ('ring forgotten at expiry plus ten minutes', '*expiry >= now.saturating_sub(600_000)', '*expiry > now.saturating_sub(600_000)'),
]:
    PLANTS.append((F, 'android', what, old, new))
for key in ('title', 'subtitle', 'body'):
    PLANTS.append((F, 'android', 'alert drops ' + key, 'for key in ["title", "body", "subtitle"]',
                   'for key in [' + ', '.join('"' + k + '"' for k in ('title','body','subtitle') if k != key) + ']'))
for key in ('from','notification','message_type','collapse_key'):
    old = '"from" | "notification" | "message_type" | "collapse_key"'
    PLANTS.append((F,'android','reserved key accepted: '+key,old,old.replace('"'+key+'"','"other-key"')))
for src,dst in [('sound','sound'),('category','category'),('thread-id','thread_id'),('interruption-level','interruption_level')]:
    PLANTS.append((F,'android','wrong Android field for '+src, f'("{src}", "{dst}")',f'("{src}", "wrong")'))
for code in ('QUOTA_EXCEEDED','INTERNAL','UNAVAILABLE'):
    old = '"QUOTA_EXCEEDED" | "UNAVAILABLE" | "INTERNAL"'
    PLANTS.append((F,'android','no retry on '+code,old,old.replace(code,'OTHER')))
for what, old, new in [
    ('activity base ignores mode', 'Routes::new(mode, account, account, key)', 'Routes::new("relay", account, account, key)'),
    ('uncertain reservation is replayed', 'self.fallback && !uncertain', 'self.fallback'),
    ('definite outage never switches', 'self.fallback && !uncertain', 'false'),
    ('unreachable relay is retried', 'self.relay_down = true;', 'self.relay_down = false;'),
]:
    PLANTS.append((K,'activity',what,old,new))

for what, old, new in [
    ('the exact refill is insufficient','if level < 1.0','if level <= 1.0'),
    ('daily cap one send late','self.count >= 300','self.count > 300'),
    ('daily cap one send early','self.count >= 300','self.count >= 299'),
    ('an end spends a burst token','if update {','if true {'),
    ('day change retains spent count','self.count = 0;', 'self.count = 300;'),
    ('one more free burst token','self.bucket.map_or(10.0','self.bucket.map_or(11.0'),
    ('one fewer free burst token','self.bucket.map_or(10.0','self.bucket.map_or(9.0'),
    ('update never debits bucket','level - 1.0, now','level, now'),
    ('day never fills','self.count.saturating_add(1)','self.count'),
    ('bucket refills twice as fast','as f64 / 288_000.0','as f64 / 144_000.0'),
    ('end at exact overload boundary prunes','elapsed <= std::time','elapsed < std::time'),
    ('three tickets allowed under pressure','decisions.len() <= 2','decisions.len() <= 3'),
    ('one ticket allowed under pressure','decisions.len() <= 2','decisions.len() <= 1'),
    ('waiting decision dropped before reply','position(|decision| !decision)','position(|decision| decision)'),
    ('second oldest waiting decision dropped','position(|decision| !decision).unwrap_or(0)','position(|decision| !decision).unwrap_or(1)'),
]:
    PLANTS.append((F,'android',what,old,new))
A='clarp-host/src/fcm.rs'
for what, old, new in [
    ('OAuth assertion has wrong algorithm','"alg":"RS256"','"alg":"RS512"'),
    ('OAuth assertion omits JWT type','"typ":"JWT"','"typ":"other"'),
    ('assertion omits key id','if !self.id.kid.is_empty()','if false'),
    ('assertion signed for another email','"iss":self.id.email','"iss":"other"'),
    ('assertion audience wrong','"aud":self.id.issuer','"aud":"other"'),
    ('assertion issued late','"iat":now','"iat":now+1'),
    ('assertion expires early','"exp":now+3600','"exp":now+3599'),
    ('assertion scope wrong','"scope":SCOPE','"scope":"other"'),
    ('signature inverted','direct_push::base64url(&signature)','direct_push::base64url(&[0;256])'),
    ('wrong token exchange grant','jwt-bearer&assertion=', 'another-grant&assertion='),
    ('wrong credential bearer','&format!("Bearer {bearer}")','&format!("Other {bearer}")'),
    ('change intended project path','/v1/projects/{project}/messages:send','/v1/projects/other/messages:send'),
    ('cache accepted at exact threshold','now < expires - 300.0','now <= expires - 300.0'),
    ('cache expires early','now < expires - 300.0','now < expires - 301.0'),
    ('fresh bearer reused on refusal','if !refresh','if true'),
    ('refresh refused','rules::Next::Refresh => refresh = true','rules::Next::Refresh => refresh = false'),
    ('only authorised users configured','value["type"] != "service_account"','value["type"] != "authorized_user"'),
    ('non HTTPS issuer configured','if !issuer.starts_with("https://")','if issuer.is_empty()'),
    ('no project is sufficient','text("project_id")?.into()', 'text("project_id").unwrap_or("missing").into()'),
    ('no email is sufficient','text("client_email")?.into()', 'text("client_email").unwrap_or("missing").into()'),
    ('no key is sufficient','text("private_key")?.into()', 'text("private_key").unwrap_or("missing").into()'),
    ('sent message lacks data map','self.http.deliver(&project, &bearer, body.as_bytes())','self.http.deliver(&project, &bearer, b"{}")'),
    ('gone phone counted successful','return Outcome::Dead(reason)','return Outcome::Sent'),
]:
    PLANTS.append((A,'host',what,old,new))
C='clarp-host/src/android_chip.rs'
for what,old,new in [
    ('Android sends quiet end to unseen phone','end && row["shown"] != true','end && row["shown"] == true'),
    ('end does not follow update','row["shown"] = json!(!end)','row["shown"] = json!(end)'),
    ('activity never urgent','if *high && !end','if false'),
    ('Android activity collapse group wrong','"agents-working",\n                        true','"other",\n                        true'),
    ('kind registration responds differently','kind must be agents-working','invalid kind'),
    ('registration without a token returns wrong status','StatusCode::CONFLICT','StatusCode::BAD_REQUEST'),
]:
    PLANTS.append((C,'host',what,old,new))
# The loopback endpoint guard and the routing to its local credential exchange are not
# planted: weakening either could cause a test to contact a real push provider.
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
