//! `clarp-admin`, as far as an agent uses it: writing to another agent.
//!
//! On the Python Host `clarp-admin` is the owner's tool for the whole installation, and
//! agents use one part of it: `clarp-admin prompt --to SESSION --from ME --text ...` is how
//! an agent hands work or a result to another (the skill `clarp-agent-communication`). An
//! agent on this Host calls the same command; this is what it does here. The arguments, what
//! is printed and the exit codes are the Python tool's (`bin/clarp-admin.py`, `cmd_prompt`).
//!
//! With `--server NAME` the message goes to an agent of another Host the owner named
//! ([`crate::peers`]): the Python Host beside this one, while agents move over one by one.
//!
//! Everything else `clarp-admin` does belongs to the Python installation: asked for here, it
//! says that it is not on this Host, and does nothing.

use clarp_protocol::requests::SendRequest;
use serde_json::json;

use crate::artifact_tool::parse;
use crate::edge::{self, App};
use crate::ids;
use crate::peers::{self, Failed};
use crate::tools::{NOT_HERE, ToolAnswer, ToolCall};

/// What argparse says of a command line it cannot read, and exits 2 with.
fn usage(why: &str) -> ToolAnswer {
    ToolAnswer::said(
        2,
        format!(
            "usage: clarp-admin prompt [-h] --to TO [--from FROM_SESSION] --text TEXT [--delay DELAY] \
             [--origin {{automation,watcher}}] [--server SERVER]\n\
             clarp-admin prompt: error: {why}"
        ),
    )
}

fn not_here(what: &str) -> ToolAnswer {
    ToolAnswer::said(
        NOT_HERE,
        format!(
            "clarp-admin {what}: not available on this Host yet (the Rust Host). Nothing was done."
        ),
    )
}

/// `clarp-admin prompt`: a message to an agent of this Host, from an agent (`--from`) or
/// from something that runs by itself (`--origin`).
async fn prompt(app: &App, args: &[String]) -> ToolAnswer {
    let valued = [
        "--to", "--from", "--text", "--delay", "--origin", "--server",
    ];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return usage(&why),
    };
    let missing: Vec<&str> = ["--to", "--text"]
        .into_iter()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        return usage(&format!(
            "the following arguments are required: {}",
            missing.join(", ")
        ));
    }
    let origin = match parsed.option("--origin") {
        None => None,
        Some(origin @ ("automation" | "watcher")) => Some(origin),
        Some(other) => {
            return usage(&format!(
                "argument --origin: invalid choice: '{other}' (choose from 'automation', 'watcher')"
            ));
        }
    };
    let from = parsed.option("--from").filter(|from| !from.is_empty());
    if from.is_some() && origin.is_some() {
        return ToolAnswer::said(1, "--origin cannot be combined with --from");
    }
    // A message for later is not built yet. Nothing is sent in its place.
    if parsed
        .option("--delay")
        .is_some_and(|delay| !delay.is_empty())
    {
        return not_here("prompt --delay");
    }
    let request = SendRequest {
        session: parsed.option("--to").unwrap_or_default().to_string(),
        text: parsed.option("--text").unwrap_or_default().to_string(),
        // One id for the message, however often the tool has to ask.
        client_msg_id: Some(format!("clarp-admin-{}", ids::hex(8))),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some(if from.is_some() {
            "agent".to_string()
        } else {
            origin.unwrap_or("automation").to_string()
        }),
        sender: from.map(|from| json!(from)),
        ..SendRequest::default()
    };
    // For an agent of another Host the owner named: that Host's own send.
    if let Some(server) = parsed
        .option("--server")
        .filter(|server| !server.is_empty())
    {
        let mut payload = json!(request);
        payload["force_session"] = json!(true);
        return match peers::send(app, server, payload).await {
            Ok(answer) => {
                ToolAnswer::printed(0, serde_json::to_string_pretty(&answer).unwrap_or_default())
            }
            Err(Failed::Unknown(name)) => ToolAnswer::said(
                1,
                format!("clarp-admin prompt: unknown or disabled peer: {name}"),
            ),
            Err(Failed::Refused { status, detail }) => ToolAnswer::said(
                4,
                format!("clarp-admin prompt: the Host refused it (HTTP {status}): {detail}"),
            ),
            Err(Failed::Down(why)) => ToolAnswer::said(
                5,
                format!(
                    "clarp-admin prompt: {why}. It may have arrived: do not send it again \
                     without looking"
                ),
            ),
        };
    }
    match edge::send_message(app, request).await {
        Ok(sent) => match serde_json::to_string_pretty(&sent) {
            Ok(shown) => ToolAnswer::printed(0, shown),
            Err(error) => ToolAnswer::said(1, format!("clarp-admin prompt: {error}")),
        },
        Err(refused) if refused.status().is_client_error() => ToolAnswer::said(
            4,
            format!(
                "clarp-admin prompt: the Host refused it (HTTP {}): {}",
                refused.status().as_u16(),
                refused.message()
            ),
        ),
        Err(failed) => ToolAnswer::said(
            1,
            format!(
                "clarp-admin prompt: the Host could not do it: {}",
                failed.message()
            ),
        ),
    }
}

/// Answers `clarp-admin`.
pub async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    match call.argv.split_first() {
        Some((command, rest)) if command == "prompt" => prompt(app, rest).await,
        Some((command, _)) => not_here(command),
        None => ToolAnswer::said(2, "usage: clarp-admin COMMAND ..."),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn what_this_host_does_not_have_of_clarp_admin_says_so() {
        assert_eq!(not_here("update").exit, NOT_HERE);
        let said = usage("the following arguments are required: --to");
        assert_eq!(said.exit, 2);
        assert!(said.stderr.contains("{automation,watcher}"));
        assert!(
            said.stderr.ends_with(
                "clarp-admin prompt: error: the following arguments are required: --to\n"
            )
        );
    }
}
