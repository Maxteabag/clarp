#!/usr/bin/env python3
"""Mutations of the alert router and its delivery at the Host."""
import runner

RUNS = {
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
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
