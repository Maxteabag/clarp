//! What comes of a turn's token, held to the Python Host's (`tests/fixtures/turn-identity-vectors.json`, written
//! by `server-rs/tools/turn-identity-vectors.py` from `lib.turn_identity.caller_agent_id` over turns that run,
//! that ended around seven days ago, a token never given and a text that is no token). The tokens differ: that
//! Host keeps a random one per turn, this one signs what a token says ([`turn_identity::make`]). What is held is
//! the outcome for each standing of the turn; the forms of this Host's own tokens are below.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::turn_identity::{self, Claim, KEPT_MS, Standing, TURN_ENV, VERSION};
use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::Sha256;

const KEY: [u8; 32] = [7; 32];

/// The agent a token names now, where its turn stands so: what the Host reads of a token, the asking of
/// the agent's log aside.
fn caller(token: &str, standing: Standing, now_ms: i64) -> Option<String> {
    let claim = turn_identity::read(&KEY, token)?;
    turn_identity::current(standing, now_ms).then_some(claim.agent_id)
}

#[test]
fn a_token_names_its_agent_while_its_turn_runs_and_seven_days_after_as_the_python_host_says() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/turn-identity-vectors.json")).unwrap();
    assert_eq!(vectors["env"], TURN_ENV);
    assert_eq!(vectors["kept_ms"], KEPT_MS);
    let now = vectors["now_ms"].as_i64().unwrap();
    let cases = vectors["cases"].as_array().unwrap();
    let token = turn_identity::make(&KEY, "id-desk", 12);
    let mut held = 0;
    for case in cases {
        let what = case["what"].as_str().unwrap();
        let theirs = case["outcome"].as_str().unwrap();
        let ours = match case["standing"].as_str().unwrap() {
            "live" => caller(&token, Standing::Live, now),
            "ended" => {
                let ago = case["ended_ago_ms"].as_i64().unwrap();
                caller(&token, Standing::Ended { at_ms: now - ago }, now)
            }
            "unknown" => caller(&token, Standing::Unknown, now),
            "no token" => {
                assert_eq!(turn_identity::read(&KEY, "not a token"), None, "{what}");
                turn_identity::read(&KEY, "").map(|claim| claim.agent_id)
            }
            // The kernel is not asked where the Host cannot read its key or the agent's log: the Host
            // answers that it cannot say, a 503, as the Python Host does (its Host test holds it).
            "unreadable" => {
                assert_eq!(theirs, "cannot say", "{what}");
                continue;
            }
            other => panic!("{what}: a standing the test does not know: {other}"),
        };
        let ours = ours.map_or("nobody".to_string(), |agent| format!("identified {agent}"));
        assert_eq!(ours, theirs, "{what}");
        held += 1;
    }
    assert_eq!((cases.len(), held), (10, 9));
}

#[test]
fn a_token_says_whose_turn_it_is_and_nothing_else_verifies() {
    let token = turn_identity::make(&KEY, "id-omar", 3);
    let claim = Claim {
        agent_id: "id-omar".to_string(),
        turn: 3,
    };
    assert_eq!(turn_identity::read(&KEY, &token), Some(claim));
    // Opaque, but of signs a header and an environment carry as they are.
    assert!(token.bytes().all(|sign| sign.is_ascii_graphic()), "{token}");
    assert!(token.starts_with(&format!("{VERSION}.")), "{token}");
    // Each turn and each agent has a token of its own, and the same key makes the same one again.
    assert_ne!(token, turn_identity::make(&KEY, "id-omar", 4));
    assert_ne!(token, turn_identity::make(&KEY, "id-desk", 3));
    assert_eq!(token, turn_identity::make(&KEY, "id-omar", 3));
    // Another key's token names nobody.
    assert_eq!(turn_identity::read(&[8; 32], &token), None);

    let fields: Vec<&str> = token.split('.').collect();
    assert_eq!(fields.len(), 4, "{token}");
    let hex = |text: &str| -> String { text.bytes().map(|b| format!("{b:02x}")).collect() };
    let with = |at: usize, value: &str| -> String {
        let mut changed = fields.clone();
        changed[at] = value;
        changed.join(".")
    };
    // Another agent's id, or another turn's number, under this MAC: nobody.
    assert_eq!(turn_identity::read(&KEY, &with(1, &hex("id-desk"))), None);
    assert_eq!(turn_identity::read(&KEY, &with(2, "4")), None);
    // Another version, though its MAC is right for what it says: signed with the same key over
    // its own text, as a Host that made tokens of that version would sign them.
    let other_version = format!("2.{}.{}", fields[1], fields[2]);
    let signed = <Hmac<Sha256> as Mac>::new_from_slice(&KEY).unwrap();
    let mac = signed.chain_update(other_version.as_bytes()).finalize();
    let mac: String = mac
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(
        turn_identity::read(&KEY, &format!("{other_version}.{mac}")),
        None
    );
    // The same signing over the version this Host reads is its own token: the case above is
    // refused for its version alone.
    let own = <Hmac<Sha256> as Mac>::new_from_slice(&KEY).unwrap();
    let said = format!("1.{}.{}", fields[1], fields[2]);
    let mac = own.chain_update(said.as_bytes()).finalize();
    let mac: String = mac
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(format!("{said}.{mac}"), token);
    // A MAC changed in one sign, cut short, in capitals, or missing.
    let last = if fields[3].ends_with('0') { "1" } else { "0" };
    let mac = format!("{}{last}", &fields[3][..63]);
    assert_eq!(turn_identity::read(&KEY, &with(3, &mac)), None);
    assert_eq!(turn_identity::read(&KEY, &with(3, &fields[3][..62])), None);
    assert_eq!(
        turn_identity::read(&KEY, &with(3, &fields[3].to_uppercase())),
        None
    );
    assert_eq!(turn_identity::read(&KEY, &fields[..3].join(".")), None);
    // A field more, or fewer.
    assert_eq!(turn_identity::read(&KEY, &format!("{token}.x")), None);
    assert_eq!(turn_identity::read(&KEY, &format!("1.{}", fields[3])), None);
    // A token with blanks around it is no token.
    assert_eq!(turn_identity::read(&KEY, &format!(" {token}")), None);
    assert_eq!(turn_identity::read(&KEY, &format!("{token}\n")), None);
}

#[test]
fn a_turn_names_its_agent_up_to_seven_days_after_it_ended_and_never_one_the_log_does_not_know() {
    let ended = 5_000;
    assert!(turn_identity::current(Standing::Live, i64::MAX));
    assert!(turn_identity::current(
        Standing::Ended { at_ms: ended },
        ended
    ));
    assert!(turn_identity::current(
        Standing::Ended { at_ms: ended },
        ended + KEPT_MS
    ));
    assert!(!turn_identity::current(
        Standing::Ended { at_ms: ended },
        ended + KEPT_MS + 1
    ));
    assert!(!turn_identity::current(Standing::Unknown, ended));
}
