//! Experience watcher signals that do not already have a Host journal.

use std::collections::{BTreeMap, VecDeque};
use std::sync::Mutex;

use clarp_protocol::requests::SendRequest;
use clarp_protocol::responses::SendResponse;
use serde_json::json;

use crate::coordination::Made;
use crate::edge::{self, ApiError, App};

const SENDER: &str = "experience-watch";
const MAX_EVENTS: usize = 32_768;
const KEEP_MS: i64 = 24 * 60 * 60 * 1_000;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Relaunches {
    pub count: usize,
    pub platform: String,
    pub build: String,
    pub contract: Option<i64>,
}

struct Event<T> {
    at: i64,
    subject: String,
    detail: T,
}

#[derive(Default)]
pub struct Signals {
    launches: Mutex<VecDeque<Event<Relaunches>>>,
    refusals: Mutex<VecDeque<Event<()>>>,
}

fn append<T>(ring: &Mutex<VecDeque<Event<T>>>, at: i64, subject: &str, detail: T) {
    let Ok(mut ring) = ring.lock() else {
        return;
    };
    while ring
        .front()
        .is_some_and(|first| first.at < at.saturating_sub(KEEP_MS) || ring.len() >= MAX_EVENTS)
    {
        ring.pop_front();
    }
    ring.push_back(Event {
        at,
        subject: subject.to_owned(),
        detail,
    });
}

impl Signals {
    /// Count a launch request the Host served for an authenticated device.
    pub(crate) fn launched(&self, device_id: &str, client: Option<&str>, now: i64) {
        let detail = client
            .and_then(|header| {
                let (platform, contract) = clarp_kernel::client::named(header)?;
                let (_, tail) = header.split_once('/')?;
                let (build, _) = tail.split_once(' ')?;
                Some(Relaunches {
                    count: 0,
                    platform: platform.to_ascii_lowercase(),
                    build: build.to_owned(),
                    contract: Some(contract),
                })
            })
            .unwrap_or_default();
        append(&self.launches, now, device_id, detail);
    }

    pub(crate) fn refused(&self, agent_id: &str, now: i64) {
        if !agent_id.is_empty() {
            append(&self.refusals, now, agent_id, ());
        }
    }

    pub fn relaunches(&self, since: i64, now: i64) -> BTreeMap<String, Relaunches> {
        let mut result: BTreeMap<String, Relaunches> = BTreeMap::new();
        let Ok(events) = self.launches.lock() else {
            return result;
        };
        for event in events
            .iter()
            .filter(|event| in_window(event.at, since, now))
        {
            let row = result.entry(event.subject.clone()).or_default();
            row.count += 1;
            if event.detail.contract.is_some() {
                row.platform.clone_from(&event.detail.platform);
                row.build.clone_from(&event.detail.build);
                row.contract = event.detail.contract;
            }
        }
        result
    }

    pub fn refusals(&self, since: i64, now: i64) -> BTreeMap<String, usize> {
        let mut result = BTreeMap::new();
        let Ok(events) = self.refusals.lock() else {
            return result;
        };
        for event in events
            .iter()
            .filter(|event| in_window(event.at, since, now))
        {
            *result.entry(event.subject.clone()).or_default() += 1;
        }
        result
    }
}

fn in_window(at: i64, since: i64, now: i64) -> bool {
    (since.max(now.saturating_sub(KEEP_MS))..=now).contains(&at)
}

/// The watcher is a Host-owned sender, with no provider turn or roster identity.
pub async fn tell(
    app: &App,
    session: &str,
    message_id: String,
    text: String,
) -> Result<SendResponse, ApiError> {
    let request = SendRequest {
        session: session.to_owned(),
        text,
        client_msg_id: Some(message_id),
        force_session: Some(true),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some("agent".into()),
        sender: Some(json!(SENDER)),
        ..SendRequest::default()
    };
    let made = Made {
        // The Host itself authorizes this fixed sender, as it does a local owner-token prompt.
        owner_token: true,
        ..Made::default()
    };
    edge::send_from(app, request, &made).await
}
