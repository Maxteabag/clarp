//! A Host that does not answer (the shared "Host not answering" spec, Peter's
//! go of 28 Nov 2026): which Host, since when, the most likely reason in one
//! plain sentence, what to do about it, a quiet reconnect line, and a short
//! diagnostics text free of private data. Everything here is pure; the engine
//! probes and keeps the state per Host (`clarp_engine::host_down`).
//!
//! The panel's words are each app's own, from the shared `host_down.*` keys.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::time::Duration;

use chrono::{TimeZone, Utc};
use sha2::{Digest, Sha256};

// ---- words and times ------------------------------------------------------
//
// The panel's words are the apps' own (`host_down.*` keys in the shared
// spec; on the desktop `@tr(...)` in host_down.slint). Here: the codes, and
// the English diagnostics text, which stays English everywhere.

/// How long `Back online` shows before the panel goes.
pub const BACK_ONLINE_FOR: Duration = Duration::from_secs(2);

/// A probe waits this long for the Host's answer.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(8);

/// "2 hours ago" for `{when}` (`host_down.last_seen`, `host_down.list_row`),
/// and the one place the desktop words a relative time until its locale
/// formatter lands (desktop-rs docs/i18n.md): clarp-i18n swaps this out.
pub fn ago(elapsed_ms: i64) -> String {
    let seconds = (elapsed_ms / 1000).max(0);
    let unit = |n: i64, one: &str| if n == 1 { format!("1 {one} ago") } else { format!("{n} {one}s ago") };
    match seconds {
        0..45 => "just now".to_owned(),
        45..3600 => unit((seconds / 60).max(1), "minute"),
        3600..86_400 => unit(seconds / 3600, "hour"),
        _ => unit(seconds / 86_400, "day"),
    }
}

/// "in 30 seconds" for `{in}` (`host_down.status.retry_in`); see `ago`.
pub fn within(left: Duration) -> String {
    let seconds = (left.as_secs() + u64::from(left.subsec_nanos() > 0)).max(1);
    if seconds < 60 {
        if seconds == 1 { "in 1 second".to_owned() } else { format!("in {seconds} seconds") }
    } else {
        let minutes = seconds.div_ceil(60);
        if minutes == 1 { "in 1 minute".to_owned() } else { format!("in {minutes} minutes") }
    }
}

// ---- reasons ----------------------------------------------------------------

/// The most likely reason a Host does not answer; the first that matches.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    DeviceOffline,
    AuthExpired,
    WrongAddress,
    ServerStopped,
    TailscaleOff,
    HostAsleep,
    Unknown,
}

/// The reason-specific button beside Try again.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    PairAgain,
    EditAddress,
    CheckTailscale,
}

impl Action {
    pub fn id(self) -> &'static str {
        match self {
            Action::PairAgain => "pair",
            Action::EditAddress => "address",
            Action::CheckTailscale => "tailscale",
        }
    }
}

impl Reason {
    pub fn id(self) -> &'static str {
        match self {
            Reason::DeviceOffline => "device_offline",
            Reason::AuthExpired => "auth_expired",
            Reason::WrongAddress => "wrong_address",
            Reason::ServerStopped => "server_stopped",
            Reason::TailscaleOff => "tailscale_off",
            Reason::HostAsleep => "host_asleep",
            Reason::Unknown => "unknown",
        }
    }

    /// The body's English (`host_down.<reason>.body`), the Host named "the
    /// Host": for the diagnostics text only, never the panel.
    pub fn english_body(self) -> &'static str {
        match self {
            Reason::DeviceOffline => "Clarp will reconnect when the network is back.",
            Reason::AuthExpired => "This device is no longer signed in to the Host.",
            Reason::WrongAddress => "The address may have changed.",
            Reason::ServerStopped => "The computer is on, but Clarp isn't running on it.",
            Reason::TailscaleOff => "Tailscale doesn't seem to be connected on this device.",
            Reason::HostAsleep => "The computer seems to be off or asleep.",
            Reason::Unknown => "The Host isn't responding right now.",
        }
    }

    /// The shared severity: warning (yellow) where the user must act,
    /// info (grey) otherwise; never error (red).
    pub fn warning(self) -> bool {
        matches!(self, Reason::AuthExpired | Reason::WrongAddress)
    }

    pub fn action(self) -> Option<Action> {
        match self {
            Reason::AuthExpired => Some(Action::PairAgain),
            Reason::WrongAddress => Some(Action::EditAddress),
            Reason::TailscaleOff => Some(Action::CheckTailscale),
            _ => None,
        }
    }

    /// The panel's icon: moon for asleep, plug for stopped, key for sign-in,
    /// signpost for the address, network for Tailscale, cloud-off else.
    pub fn icon(self) -> &'static str {
        match self {
            Reason::HostAsleep => "moon",
            Reason::ServerStopped => "plug",
            Reason::AuthExpired => "key",
            Reason::WrongAddress => "signpost",
            Reason::TailscaleOff => "network",
            Reason::DeviceOffline | Reason::Unknown => "cloud-off",
        }
    }
}

/// How the last attempt to reach the Host ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Attempt {
    /// Nothing answered within this long.
    Timeout(Duration),
    /// The machine answered and refused the connection.
    Refused,
    /// No route to the address (the network said so at once).
    NoRoute,
    /// The name did not resolve.
    NameNotFound,
    /// The secure connection failed (a certificate for another name).
    Tls,
    /// The Host answered with this status.
    Http(u16),
    /// Something answered, and it is not this Host.
    NotThisHost,
    /// Anything else.
    Other,
}

impl Attempt {
    /// The diagnostics line's words. Never the raw error: it can name the
    /// address.
    pub fn describe(&self) -> String {
        match self {
            Attempt::Timeout(after) => format!("timeout after {} s", after.as_secs().max(1)),
            Attempt::Refused => "connection refused".into(),
            Attempt::NoRoute => "no route to host".into(),
            Attempt::NameNotFound => "name not found".into(),
            Attempt::Tls => "TLS error".into(),
            Attempt::Http(status) => format!("HTTP {status}"),
            Attempt::NotThisHost => "not this Host".into(),
            Attempt::Other => "other error".into(),
        }
    }

    fn unanswered(&self) -> bool {
        matches!(self, Attempt::Timeout(_) | Attempt::NoRoute)
    }
}

/// What else is known when the attempt failed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Context {
    /// The operating system's word on the network: Some(false) when this
    /// device has none at all.
    pub device_online: Option<bool>,
    /// Whether this device has a Tailscale (or other VPN) interface up.
    pub tailscale_on_device: Option<bool>,
    pub address: AddressKind,
    /// Other Hosts this device reads, and how many of them answer.
    pub other_hosts: usize,
    pub other_hosts_answering: usize,
    /// Of those, the ones at tailnet addresses.
    pub other_tailnet_hosts: usize,
    pub other_tailnet_hosts_answering: usize,
}

/// The spec's classifier: from the last failed attempt plus context, the
/// first reason that matches.
pub fn classify(attempt: &Attempt, context: &Context) -> Reason {
    if context.device_online == Some(false) {
        return Reason::DeviceOffline;
    }
    match attempt {
        Attempt::Http(401 | 403) => return Reason::AuthExpired,
        Attempt::NameNotFound | Attempt::Tls | Attempt::NotThisHost | Attempt::Http(404) => return Reason::WrongAddress,
        Attempt::Refused => return Reason::ServerStopped,
        _ => {}
    }
    if attempt.unanswered() {
        let tailnet = context.address == AddressKind::Tailnet;
        let no_tailnet_peer_answers = context.other_tailnet_hosts > 0 && context.other_tailnet_hosts_answering == 0;
        if tailnet && (context.tailscale_on_device == Some(false) || no_tailnet_peer_answers) {
            return Reason::TailscaleOff;
        }
        // Another Host answers, or Tailscale is up and this tailnet address
        // alone stays silent: the computer itself is away.
        if context.other_hosts_answering > 0 || (tailnet && context.tailscale_on_device == Some(true)) {
            return Reason::HostAsleep;
        }
    }
    Reason::Unknown
}

// ---- addresses --------------------------------------------------------------

/// What kind of address a Host has (for the classifier and diagnostics; the
/// address itself never leaves this device in the diagnostics text).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AddressKind {
    Tailnet,
    Lan,
    Loopback,
    Public,
    #[default]
    Unknown,
}

impl AddressKind {
    pub fn id(self) -> &'static str {
        match self {
            AddressKind::Tailnet => "tailnet",
            AddressKind::Lan => "lan",
            AddressKind::Loopback => "loopback",
            AddressKind::Public => "public",
            AddressKind::Unknown => "unknown",
        }
    }
}

/// 100.64.0.0/10 (Tailscale's CGNAT range).
pub fn is_tailnet_v4(ip: Ipv4Addr) -> bool {
    let [a, b, ..] = ip.octets();
    a == 100 && (64..128).contains(&b)
}

/// fd7a:115c:a1e0::/48 (Tailscale's IPv6 range).
pub fn is_tailnet_v6(ip: Ipv6Addr) -> bool {
    let s = ip.segments();
    s[0] == 0xfd7a && s[1] == 0x115c && s[2] == 0xa1e0
}

pub fn is_tailnet_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_tailnet_v4(v4),
        IpAddr::V6(v6) => is_tailnet_v6(v6),
    }
}

/// The kind of a Host's base URL.
pub fn address_kind(base_url: &str) -> AddressKind {
    let Ok(url) = url::Url::parse(base_url) else { return AddressKind::Unknown };
    match url.host() {
        Some(url::Host::Ipv4(ip)) => ip_kind(IpAddr::V4(ip)),
        Some(url::Host::Ipv6(ip)) => ip_kind(IpAddr::V6(ip)),
        Some(url::Host::Domain(name)) => {
            let name = name.trim_end_matches('.').to_ascii_lowercase();
            if name == "localhost" || name.ends_with(".localhost") {
                AddressKind::Loopback
            } else if name.ends_with(".ts.net") {
                AddressKind::Tailnet
            } else if name.ends_with(".local") || name.ends_with(".lan") || name.ends_with(".home.arpa") || !name.contains('.') {
                AddressKind::Lan
            } else {
                AddressKind::Public
            }
        }
        None => AddressKind::Unknown,
    }
}

fn ip_kind(ip: IpAddr) -> AddressKind {
    if ip.is_loopback() {
        return AddressKind::Loopback;
    }
    if is_tailnet_ip(ip) {
        return AddressKind::Tailnet;
    }
    let lan = match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_link_local(),
        IpAddr::V6(v6) => (v6.segments()[0] & 0xfe00) == 0xfc00 || (v6.segments()[0] & 0xffc0) == 0xfe80,
    };
    if lan { AddressKind::Lan } else { AddressKind::Public }
}

/// `http` or `https` (anything else: `unknown`).
pub fn scheme(base_url: &str) -> &'static str {
    match url::Url::parse(base_url).map(|u| u.scheme().to_owned()).as_deref() {
        Ok("http") => "http",
        Ok("https") => "https",
        _ => "unknown",
    }
}

// ---- reconnect ----------------------------------------------------------------

/// Probes after a failure wait 2 s, 5 s, 10 s, 30 s, then every 60 s.
pub fn backoff(failures: u32) -> Duration {
    const STEPS: [u64; 4] = [2, 5, 10, 30];
    Duration::from_secs(STEPS.get(failures.saturating_sub(1) as usize).copied().unwrap_or(60))
}

// ---- diagnostics ----------------------------------------------------------------

/// The Host as diagnostics name it: `#` and the first 6 hex of the sha256 of
/// its id (or base URL when its id is not known).
pub fn host_hash(id_or_url: &str) -> String {
    let digest = Sha256::digest(id_or_url.as_bytes());
    let hex: String = digest.iter().take(3).map(|b| format!("{b:02x}")).collect();
    format!("#{hex}")
}

/// The device's network as diagnostics say it.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeviceNetwork {
    /// None: the operating system could not be asked.
    pub online: Option<bool>,
    /// `wifi`, `ethernet`, `cellular` or `vpn`, when known.
    pub kind: Option<&'static str>,
    pub tailscale: Option<bool>,
}

impl DeviceNetwork {
    fn describe(&self) -> String {
        let network = match (self.online, self.kind) {
            (Some(true), Some(kind)) => format!("online ({kind})"),
            (Some(true), None) => "online".into(),
            (Some(false), _) => "offline".into(),
            (None, _) => "unknown".into(),
        };
        let tailscale = match self.tailscale {
            Some(true) => "yes",
            Some(false) => "no",
            None => "unknown",
        };
        format!("Device network: {network} · Tailscale/VPN on device: {tailscale}")
    }
}

/// What the diagnostics text is made of; none of it private.
#[derive(Debug, Clone)]
pub struct Diagnostics<'a> {
    pub platform: &'a str,
    pub version: &'a str,
    pub build: &'a str,
    /// Hashed; never written as it is.
    pub host_id_or_url: &'a str,
    /// Only its kind and scheme are written.
    pub base_url: &'a str,
    pub host_version: &'a str,
    pub contract: Option<u32>,
    pub down_since_ms: i64,
    pub now_ms: i64,
    pub reason: Reason,
    pub last_attempt: Option<&'a Attempt>,
    pub attempts: u32,
    pub next_in: Option<Duration>,
    pub others_answering: usize,
    pub others: usize,
    pub network: DeviceNetwork,
}

fn iso_utc(ms: i64) -> String {
    Utc.timestamp_millis_opt(ms).single().map_or_else(|| "unknown".into(), |at| at.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
}

/// `2 h 5 min`, `3 min`, `40 s`, `1 d 2 h`.
fn duration_text(ms: i64) -> String {
    let seconds = (ms / 1000).max(0);
    let (days, hours, minutes) = (seconds / 86_400, seconds % 86_400 / 3600, seconds % 3600 / 60);
    if days > 0 {
        format!("{days} d {hours} h")
    } else if hours > 0 {
        format!("{hours} h {minutes} min")
    } else if minutes > 0 {
        format!("{minutes} min")
    } else {
        format!("{seconds} s")
    }
}

/// The spec's plain-text diagnostics (at most 15 lines): no token, address,
/// name, path or content; the Host by its hash only.
pub fn diagnostics(d: &Diagnostics) -> String {
    let host_version = if d.host_version.is_empty() {
        "unknown".to_owned()
    } else {
        match d.contract {
            Some(contract) => format!("{} · contract {contract}", d.host_version),
            None => d.host_version.to_owned(),
        }
    };
    let sentence = d.reason.english_body();
    let next = d.next_in.map_or_else(|| "now".to_owned(), |left| format!("{} s", left.as_secs()));
    [
        "Clarp diagnostics".to_owned(),
        format!("App: {} {} ({})", d.platform, d.version, d.build),
        format!(
            "Host: {} · address kind: {} · scheme: {}",
            host_hash(d.host_id_or_url),
            address_kind(d.base_url).id(),
            scheme(d.base_url)
        ),
        format!("Host version: {host_version}"),
        format!("State: not answering since {} ({})", iso_utc(d.down_since_ms), duration_text(d.now_ms - d.down_since_ms)),
        format!("Likely reason: {} — {sentence}", d.reason.id()),
        format!("Last attempt: {}", d.last_attempt.map_or_else(|| "none yet".to_owned(), Attempt::describe)),
        format!("Attempts since down: {} · next in {next}", d.attempts),
        format!("Other Hosts answering: {} of {}", d.others_answering, d.others),
        d.network.describe(),
        format!("Time: {}", iso_utc(d.now_ms)),
    ]
    .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn context(address: AddressKind) -> Context {
        Context { address, device_online: Some(true), ..Context::default() }
    }

    #[test]
    fn the_first_matching_reason_wins() {
        let timeout = Attempt::Timeout(Duration::from_secs(8));
        let mut offline = context(AddressKind::Tailnet);
        offline.device_online = Some(false);
        assert_eq!(classify(&Attempt::Http(401), &offline), Reason::DeviceOffline);
        let lan = context(AddressKind::Lan);
        assert_eq!(classify(&Attempt::Http(401), &lan), Reason::AuthExpired);
        assert_eq!(classify(&Attempt::Http(403), &lan), Reason::AuthExpired);
        for wrong in [Attempt::NameNotFound, Attempt::Tls, Attempt::NotThisHost, Attempt::Http(404)] {
            assert_eq!(classify(&wrong, &lan), Reason::WrongAddress, "{wrong:?}");
        }
        assert_eq!(classify(&Attempt::Refused, &context(AddressKind::Tailnet)), Reason::ServerStopped);
        assert_eq!(classify(&Attempt::Http(503), &lan), Reason::Unknown);
        assert_eq!(classify(&timeout, &lan), Reason::Unknown, "nothing else answers: no guess");
    }

    #[test]
    fn a_silent_tailnet_address_is_tailscale_or_sleep() {
        let timeout = Attempt::Timeout(Duration::from_secs(8));
        let mut no_tailscale = context(AddressKind::Tailnet);
        no_tailscale.tailscale_on_device = Some(false);
        assert_eq!(classify(&timeout, &no_tailscale), Reason::TailscaleOff);
        assert_eq!(classify(&Attempt::NoRoute, &no_tailscale), Reason::TailscaleOff);

        // Tailscale is up, but no other tailnet Host answers either.
        let mut none_answer = context(AddressKind::Tailnet);
        none_answer.tailscale_on_device = Some(true);
        none_answer.other_tailnet_hosts = 1;
        assert_eq!(classify(&timeout, &none_answer), Reason::TailscaleOff);

        // Another tailnet Host answers: this computer is away.
        let mut other_answers = none_answer.clone();
        other_answers.other_hosts = 1;
        other_answers.other_hosts_answering = 1;
        other_answers.other_tailnet_hosts_answering = 1;
        assert_eq!(classify(&timeout, &other_answers), Reason::HostAsleep);

        // Tailscale up, the only Host: asleep.
        let mut alone = context(AddressKind::Tailnet);
        alone.tailscale_on_device = Some(true);
        assert_eq!(classify(&timeout, &alone), Reason::HostAsleep);

        // A LAN Host silent while another Host answers.
        let mut lan = context(AddressKind::Lan);
        lan.other_hosts = 1;
        lan.other_hosts_answering = 1;
        assert_eq!(classify(&timeout, &lan), Reason::HostAsleep);
        // A refusal is never Tailscale.
        assert_eq!(classify(&Attempt::Refused, &no_tailscale), Reason::ServerStopped);
    }

    #[test]
    fn reasons_carry_their_action_icon_and_severity() {
        assert_eq!(Reason::TailscaleOff.action().map(Action::id), Some("tailscale"));
        assert_eq!(Reason::AuthExpired.action().map(Action::id), Some("pair"));
        assert_eq!(Reason::WrongAddress.action().map(Action::id), Some("address"));
        assert_eq!(Reason::HostAsleep.action(), None);
        assert!(Reason::AuthExpired.warning() && Reason::WrongAddress.warning());
        for info in [Reason::DeviceOffline, Reason::ServerStopped, Reason::TailscaleOff, Reason::HostAsleep, Reason::Unknown] {
            assert!(!info.warning(), "{info:?} is info grey");
        }
        assert_eq!(Reason::ServerStopped.icon(), "plug");
        assert_eq!(Reason::HostAsleep.icon(), "moon");
    }

    #[test]
    fn relative_times_read_naturally() {
        assert_eq!(ago(10_000), "just now");
        assert_eq!(ago(60_000), "1 minute ago");
        assert_eq!(ago(25 * 60_000), "25 minutes ago");
        assert_eq!(ago(2 * 3_600_000 + 5), "2 hours ago");
        assert_eq!(ago(26 * 3_600_000), "1 day ago");
        assert_eq!(within(Duration::from_millis(4200)), "in 5 seconds");
        assert_eq!(within(Duration::from_secs(1)), "in 1 second");
        assert_eq!(within(Duration::from_secs(60)), "in 1 minute");
        assert_eq!(within(Duration::from_secs(90)), "in 2 minutes");
    }

    #[test]
    fn backoff_is_2_5_10_30_then_every_minute() {
        let steps: Vec<u64> = (1..=7).map(|n| backoff(n).as_secs()).collect();
        assert_eq!(steps, [2, 5, 10, 30, 60, 60, 60]);
        assert_eq!(backoff(0).as_secs(), 2);
    }

    #[test]
    fn addresses_are_told_apart() {
        assert_eq!(address_kind("http://100.101.102.103:7690"), AddressKind::Tailnet);
        assert_eq!(address_kind("http://100.63.0.1:7690"), AddressKind::Public);
        assert_eq!(address_kind("http://100.128.0.1:7690"), AddressKind::Public);
        assert_eq!(address_kind("http://[fd7a:115c:a1e0::1]:7690"), AddressKind::Tailnet);
        assert_eq!(address_kind("https://laptopstudio.tail1234.ts.net"), AddressKind::Tailnet);
        assert_eq!(address_kind("http://127.0.0.1:7690"), AddressKind::Loopback);
        assert_eq!(address_kind("http://localhost:7690"), AddressKind::Loopback);
        assert_eq!(address_kind("http://192.168.1.20:7690"), AddressKind::Lan);
        assert_eq!(address_kind("http://elitebook:7690"), AddressKind::Lan);
        assert_eq!(address_kind("https://clarp.example.com"), AddressKind::Public);
        assert_eq!(address_kind("not a url"), AddressKind::Unknown);
        assert_eq!(scheme("https://x.example"), "https");
        assert_eq!(scheme("http://x"), "http");
    }

    #[test]
    fn diagnostics_hold_nothing_private() {
        let base = "http://100.101.102.103:7690";
        let attempt = Attempt::Timeout(Duration::from_secs(8));
        let text = diagnostics(&Diagnostics {
            platform: "desktop-linux",
            version: "0.6.8",
            build: "abc1234",
            host_id_or_url: "srv-laptopstudio-123",
            base_url: base,
            host_version: "2.4.0",
            contract: Some(51),
            down_since_ms: 1_764_300_000_000,
            now_ms: 1_764_300_000_000 + 3_725_000,
            reason: Reason::HostAsleep,
            last_attempt: Some(&attempt),
            attempts: 7,
            next_in: Some(Duration::from_secs(42)),
            others_answering: 1,
            others: 2,
            network: DeviceNetwork { online: Some(true), kind: Some("wifi"), tailscale: Some(true) },
        });
        let lines: Vec<&str> = text.lines().collect();
        assert!(lines.len() <= 15, "{text}");
        assert_eq!(lines[0], "Clarp diagnostics");
        assert_eq!(lines[1], "App: desktop-linux 0.6.8 (abc1234)");
        assert_eq!(lines[2], format!("Host: {} · address kind: tailnet · scheme: http", host_hash("srv-laptopstudio-123")));
        assert!(lines[2].starts_with("Host: #") && lines[2][7..13].chars().all(|c| c.is_ascii_hexdigit()));
        assert_eq!(lines[3], "Host version: 2.4.0 · contract 51");
        assert!(lines[4].starts_with("State: not answering since 2025-") && lines[4].ends_with("(1 h 2 min)"), "{}", lines[4]);
        assert_eq!(lines[5], "Likely reason: host_asleep — The computer seems to be off or asleep.");
        assert_eq!(lines[6], "Last attempt: timeout after 8 s");
        assert_eq!(lines[7], "Attempts since down: 7 · next in 42 s");
        assert_eq!(lines[8], "Other Hosts answering: 1 of 2");
        assert_eq!(lines[9], "Device network: online (wifi) · Tailscale/VPN on device: yes");
        assert!(lines[10].starts_with("Time: "));
        for private in ["100.101", "7690", "laptopstudio", "srv-", "://"] {
            assert!(!text.contains(private), "diagnostics leak {private}: {text}");
        }
        let auth = diagnostics(&Diagnostics { reason: Reason::AuthExpired, host_version: "", ..diagnostics_input(base) });
        assert!(auth.contains("Likely reason: auth_expired — This device is no longer signed in to the Host."));
        assert!(auth.contains("Host version: unknown"));
    }

    fn diagnostics_input(base: &str) -> Diagnostics<'_> {
        Diagnostics {
            platform: "desktop-linux",
            version: "0",
            build: "0",
            host_id_or_url: base,
            base_url: base,
            host_version: "",
            contract: None,
            down_since_ms: 0,
            now_ms: 0,
            reason: Reason::Unknown,
            last_attempt: None,
            attempts: 0,
            next_in: None,
            others_answering: 0,
            others: 0,
            network: DeviceNetwork::default(),
        }
    }
}
