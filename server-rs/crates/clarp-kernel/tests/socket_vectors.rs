//! Whether a request asks for a socket, against the Python Host's own answer.
//!
//! `fixtures/socket-vectors.json` is written by `server-rs/tools/socket-vectors.py` from
//! `server/lib/ws.py`: what `is_websocket_upgrade` answers for every combination of what
//! the three headers it looks at may say, and for requests that say a header twice or in
//! letters of their own. The same requests must be read the same way here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::socket::asked_for;
use serde_json::Value;

#[test]
fn a_request_asks_for_a_socket_exactly_when_the_python_host_says_so() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/socket-vectors.json")).unwrap();
    let said = |name: &str| -> Vec<Option<String>> {
        let said = vectors[name].as_array().unwrap().iter();
        said.map(|value| value.as_str().map(str::to_string))
            .collect()
    };
    let (upgrade, connection, key) = (said("upgrade"), said("connection"), said("key"));
    let table = vectors["table"].as_str().unwrap().as_bytes();
    assert_eq!(table.len(), upgrade.len() * connection.len() * key.len());
    let asking = table.iter().filter(|sign| **sign == b'1').count();
    assert_eq!((table.len(), asking), (1309, 90));

    let mut wrong: Vec<String> = Vec::new();
    for (case, sign) in table.iter().enumerate() {
        // The generator's order: the header that is listed last changes fastest.
        let picked = [
            ("Upgrade", &upgrade[case / (connection.len() * key.len())]),
            (
                "Connection",
                &connection[case / key.len() % connection.len()],
            ),
            ("Sec-WebSocket-Key", &key[case % key.len()]),
        ];
        let headers: Vec<(String, String)> = picked
            .iter()
            .filter_map(|(name, value)| Some(((*name).to_string(), (*value).clone()?)))
            .collect();
        if asked_for(&headers) != (*sign == b'1') {
            wrong.push(format!("{headers:?}"));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {} are read otherwise, the first: {:?}",
        wrong.len(),
        table.len(),
        &wrong[..wrong.len().min(5)]
    );

    let odd = vectors["odd"].as_array().unwrap();
    let mut asking = 0;
    for case in odd {
        let headers: Vec<(String, String)> = case["headers"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| {
                let part = |at: usize| pair[at].as_str().unwrap().to_string();
                (part(0), part(1))
            })
            .collect();
        let asks = case["asks"].as_bool().unwrap();
        assert_eq!(asked_for(&headers), asks, "{headers:?}");
        asking += usize::from(asks);
    }
    assert_eq!((odd.len(), asking), (14, 8));
}
