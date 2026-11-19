//! Preconditions for a bookkeeping delegation. The two active role indexes
//! are checked by the fleet in the same operation that reserves them.
use crate::python;

pub struct Participant<'a> {
    pub id: &'a str,
    pub parent: Option<&'a str>,
    pub role: &'a str,
    pub backend: &'a str,
    pub archived_or_deleted: bool,
}

pub fn reason(raw: &str) -> Result<&str, &'static str> {
    let text = python::strip(raw);
    if text.is_empty() {
        return Err("reason is required");
    }
    if text.chars().count() > 500 {
        return Err("reason is longer than 500 characters");
    }
    Ok(text)
}

pub fn carries_identity(backend: &str) -> bool {
    matches!(backend, "claude" | "agy" | "grok" | "opencode" | "deepseek")
}

pub fn enable<'a>(
    principal: &Participant<'_>,
    delegate: &Participant<'_>,
    raw_reason: &'a str,
    baseline: i64,
) -> Result<&'a str, String> {
    if !(0..=2000).contains(&baseline) {
        return Err("baseline_messages is 0-2000".into());
    }
    let why = reason(raw_reason)?;
    if principal.id == delegate.id {
        return Err("an agent cannot keep its own books".into());
    }
    if delegate.parent != Some(principal.id) || delegate.role != "helper" {
        return Err("the delegate must be a helper whose parent is the principal".into());
    }
    if delegate.archived_or_deleted {
        return Err("the delegate is archived".into());
    }
    for (role, agent) in [("principal", principal), ("delegate", delegate)] {
        if !carries_identity(agent.backend) {
            return Err(format!(
                "the {role} runs on {}, whose turns carry no Clarp identity; the scope could not be enforced",
                agent.backend
            ));
        }
    }
    Ok(why)
}

/// When a principal has an active delegate, the trusted service paths can
/// still change its goal. An agent proves its identity and its lineage must
/// lie outside the bookkeeping helper's subtree. A peer that cannot carry a
/// token has the narrower exception for recording its own reply.
pub fn guard(
    kind: &str,
    verified: bool,
    descendant: bool,
    backend: Option<&str>,
) -> Result<(), &'static str> {
    if matches!(kind, "user" | "system" | "legacy") {
        return Ok(());
    }
    if kind == "delegate" {
        return Err("a bookkeeping delegate cannot change its principal's goal");
    }
    if kind == "peer" && !verified {
        if !descendant && backend.is_some_and(|s| !carries_identity(s)) {
            return Ok(());
        }
        return Err(
            "this goal has a bookkeeping delegate; the reply must come from the replier's own Clarp turn",
        );
    }
    if !verified {
        return Err(
            "this goal has a bookkeeping delegate, so changes need the caller's Clarp turn identity; run the command from the owner's own turn",
        );
    }
    if descendant {
        return Err(
            "a bookkeeping delegate (or an agent it started) cannot change its principal's goal; use `clarp-goal bookkeeping record`",
        );
    }
    Ok(())
}
