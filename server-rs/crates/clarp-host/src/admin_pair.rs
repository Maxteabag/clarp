//! The owner CLI for one-time device pairing.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};

use serde_json::{Value, json};
use url::{Host, Url};

use crate::artifact_tool::{Parsed, parse};
use crate::edge::{self, ApiError, App, IssueRequest};
use crate::owner::{OwnerFile, text_at};
use crate::pairing::DEFAULT_TTL_SECONDS;
use crate::tools::ToolAnswer;

const CREATE: &str = "usage: clarp-admin pair create [-h] [--url URL] [--relay] [--name NAME] \
    [--scope {full,limited}] [--ttl TTL] [--allow-loopback] [--json]";
const LIST: &str = "usage: clarp-admin pair list [-h] [--all]";
const REVOKE: &str = "usage: clarp-admin pair revoke [-h] device_id";
const VALID_URL: &str = "pairing requires a valid http:// or https:// Computer URL";

pub(crate) async fn run(app: &App, args: &[String], owner_token: bool) -> ToolAnswer {
    if !owner_token {
        return ToolAnswer::said(1, "clarp-admin pair: owner token required");
    }
    match args.split_first() {
        Some((command, rest)) if command == "create" => create(app, rest).await,
        Some((command, rest)) if command == "list" => {
            let parsed = match parse(rest, &[], &["--all"], &[]) {
                Ok(parsed) => parsed,
                Err(why) => return usage(LIST, "list", &why),
            };
            let devices = if parsed.flags.contains("--all") {
                match app.fleet.paired_device_history().await {
                    Ok(devices) => devices,
                    Err(_) => return ToolAnswer::said(1, "could not read paired devices"),
                }
            } else {
                edge::active_devices(app)
            };
            printed_json(&edge::public_devices(devices))
        }
        Some((command, rest)) if command == "revoke" => {
            let parsed = match parse(rest, &[], &[], &["device_id"]) {
                Ok(parsed) => parsed,
                Err(why) => return usage(REVOKE, "revoke", &why),
            };
            let id = &parsed.positional[0];
            match edge::revoke_pairing(app, id, None).await {
                Ok(()) => ToolAnswer::printed(0, format!("revoked: {id}")),
                Err(why) => refused(&why),
            }
        }
        _ => ToolAnswer::said(2, "usage: clarp-admin pair [-h] {create,list,revoke} ..."),
    }
}

fn usage(line: &str, command: &str, why: &str) -> ToolAnswer {
    ToolAnswer::said(
        2,
        format!("{line}\nclarp-admin pair {command}: error: {why}"),
    )
}

fn refused(why: &ApiError) -> ToolAnswer {
    ToolAnswer::said(1, format!("clarp-admin pair: {}", why.message()))
}

fn printed_json(value: &Value) -> ToolAnswer {
    match serde_json::to_string_pretty(value) {
        Ok(said) => ToolAnswer::printed(0, said),
        Err(_) => ToolAnswer::said(1, "could not print pairing JSON"),
    }
}

async fn create(app: &App, args: &[String]) -> ToolAnswer {
    let parsed = match parse(
        args,
        &["--url", "--name", "--scope", "--ttl"],
        &["--relay", "--allow-loopback", "--json"],
        &[],
    ) {
        Ok(parsed) => parsed,
        Err(why) => return usage(CREATE, "create", &why),
    };
    if parsed.flags.contains("--relay") && parsed.option("--url").is_some() {
        return usage(CREATE, "create", "use either --url or --relay");
    }
    let scope = match parsed.choice("--scope", &["full", "limited"], "full") {
        Ok(scope) => scope,
        Err(why) => return usage(CREATE, "create", &why),
    };
    let ttl = match parsed.whole("--ttl") {
        Ok(ttl) => ttl.unwrap_or(DEFAULT_TTL_SECONDS),
        Err(why) => return usage(CREATE, "create", &why),
    };
    let url = match address(app, &parsed) {
        Ok(url) => url,
        Err(why) => return ToolAnswer::said(1, why),
    };
    let request = IssueRequest {
        device_name: parsed.option("--name").unwrap_or("iPhone").to_owned(),
        scope,
        ttl_seconds: Some(ttl),
        url: Some(url),
        relay: false,
    };
    let answer = match edge::issue_pairing(app, request).await {
        Ok(answer) => answer,
        Err(why) => return refused(&why),
    };
    let Some(uri) = answer["uri"].as_str() else {
        return ToolAnswer::said(1, "the Host's answer had no pairing link");
    };
    if parsed.flags.contains("--json") {
        // The one-time code appears only in the URI. TUI reads this URI and expires_at.
        return printed_json(&json!({
            "device_name": answer["device_name"],
            "scope": answer["scope"],
            "created_at": answer["created_at"],
            "expires_at": answer["expires_at"],
            "url": answer["url"],
            "uri": uri,
        }));
    }
    use qrcode::render::unicode::Dense1x2;
    let qr = qrcode::QrCode::new(uri.as_bytes()).map_or_else(
        |_| String::new(),
        |matrix| {
            let qr = matrix
                .render::<Dense1x2>()
                .dark_color(Dense1x2::Light)
                .light_color(Dense1x2::Dark)
                .build();
            format!("{qr}\n")
        },
    );
    ToolAnswer::printed(
        0,
        format!("{qr}{uri}\nExpires in {ttl} seconds; one use only."),
    )
}

fn address(app: &App, parsed: &Parsed) -> Result<String, &'static str> {
    let raw = if parsed.flags.contains("--relay") {
        app.connector
            .pairing_url()
            .ok_or("enable the managed relay before pairing through it")?
    } else if let Some(explicit) = parsed.option("--url").filter(|url| !url.trim().is_empty()) {
        explicit.to_owned()
    } else {
        let own = crate::config::host_config_file();
        [own, app.config.speech_keys.clone()]
            .into_iter()
            .find_map(|file| {
                OwnerFile::new(file)
                    .read()
                    .and_then(|table| text_at(&table, &["server", "public_base_url"]))
            })
            .or_else(|| app.connector.pairing_url())
            .unwrap_or_else(|| {
                let listen = app.config.listen;
                let address = if listen.ip().is_unspecified() {
                    SocketAddr::new(Ipv4Addr::LOCALHOST.into(), listen.port())
                } else {
                    listen
                };
                format!("http://{address}")
            })
    };
    checked_url(&raw, parsed.flags.contains("--allow-loopback"))
}

fn checked_url(raw: &str, allow_loopback: bool) -> Result<String, &'static str> {
    let url = Url::parse(raw.trim()).map_err(|_| VALID_URL)?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(VALID_URL);
    }
    let local = match url.host().ok_or(VALID_URL)? {
        Host::Domain(name) => {
            // A .ts.net name can be public through Tailscale Funnel.
            let name = name.trim_end_matches('.');
            !name.contains('.') || name.ends_with(".localhost") || name.ends_with(".local")
        }
        Host::Ipv4(ip) => private_ip(ip.into()),
        Host::Ipv6(ip) => private_ip(ip.into()),
    };
    if local && !allow_loopback {
        return Err(
            "pairing URL is local or tailnet-only; pass --url with an address the device can reach",
        );
    }
    Ok(url.as_str().trim_end_matches('/').to_owned())
}

fn private_ip(ip: IpAddr) -> bool {
    match ip.to_canonical() {
        IpAddr::V4(ip) => {
            let [first, second, ..] = ip.octets();
            ip.is_loopback()
                || ip.is_private()
                || ip.is_link_local()
                || ip.is_unspecified()
                || (first == 100 && (64..=127).contains(&second))
        }
        IpAddr::V6(ip) => {
            ip.is_loopback()
                || ip.is_unique_local()
                || ip.is_unicast_link_local()
                || ip.is_unspecified()
        }
    }
}
