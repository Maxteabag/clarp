//! The skills a Host keeps installed for its agents' tools.
//!
//! A release of Clarp ships a tree of skills with a manifest. A Host that keeps a machine's
//! tools links each chosen skill into the folders the tools read skills from (Claude Code's,
//! Codex's, and Antigravity's where it is installed) and reports, skill by skill, how those
//! links stand. Only one Host keeps a machine's tools: the folders are the machine's, not a
//! Host's (the lead's ruling of 20 Nov 2026), so a Host started with `--accounts follow`, or
//! with no mode, reads none and changes none.
//!
//! The rules are the Python Host's (`server/lib/managed_skills.py` and the two routes of
//! `server/lib/routes/providers.py`), held to it by `tests/fixtures/managed-skills-vectors.json`,
//! which `server-rs/tools/managed-skills-vectors.py` writes. What is on a disk, where a link
//! leads and which programs are on the path are the Host's to find out; nothing here reads a
//! disk.

use serde_json::{Map, Value, json};

use crate::python;

/// The store's key for the skills this Host keeps chosen: a JSON list of their ids, sorted.
/// Its presence also says that the first list has been taken (from the other Host's list, or
/// the release's defaults), which happens once.
pub const CHOSEN_SETTING: &str = "managed_skills.chosen";

/// The word a change is refused with by a Host that does not keep this machine's tools.
pub const KEPT_ELSEWHERE: &str = "skills_kept_elsewhere";
/// And why, in one sentence.
pub const KEPT_ELSEWHERE_WHY: &str = "This Host does not keep this machine's tools; \
the change to a skill is made on the Host that does.";
/// The word a change is refused with by a Host installed without a tree of skills.
pub const NOT_SHIPPED: &str = "skills_not_shipped";
/// And why.
pub const NOT_SHIPPED_WHY: &str = "This Host was installed without the skills it would keep.";

/// What the reference says when its manifest cannot be read.
pub const UNAVAILABLE: &str = "Clarp skill manifest is unavailable";
/// What a change that names no skill or no switch is refused with.
pub const REQUIRED: &str = "skill_id and boolean enabled are required";
/// What a body that is no JSON object is refused with.
pub const BAD_JSON: &str = "bad json";

/// A folder a tool reads skills from, in the order the reference writes them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Root {
    Claude,
    Codex,
    /// Antigravity's: used only where Antigravity is installed. A skill of the user's own
    /// there is left alone and holds nothing else back.
    Agy,
}

impl Root {
    pub fn name(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
            Self::Agy => "agy",
        }
    }

    /// A folder whose conflicts skip only its own link.
    pub fn optional(self) -> bool {
        self == Self::Agy
    }
}

/// How a skill's place in one folder stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Link {
    /// Nothing is there.
    Missing,
    /// Something that is not this Host's: a folder or file, or a link to elsewhere.
    Modified,
    /// A link to this release's copy of the skill.
    Healthy,
    /// A link to the same skill in another release of Clarp: this Host's own, or the Python
    /// Host's where it kept the machine's tools before.
    Outdated,
}

impl Link {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Modified => "modified",
            Self::Healthy => "healthy",
            Self::Outdated => "outdated",
        }
    }
}

/// What is at a skill's place in a folder, as the Host found it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum At<'a> {
    Nothing,
    /// A folder or a file: not a link.
    Thing,
    /// A link, and the path it leads to, resolved as far as it goes.
    Link(&'a str),
}

/// How a place stands, given the resolved path of this release's copy of the skill (`source`)
/// and the resolved folders other releases of Clarp live in (`releases`, each holding
/// `<release>/skills/<name>`).
pub fn link_state(at: At, source: &str, releases: &[&str], name: &str) -> Link {
    let resolved = match at {
        At::Nothing => return Link::Missing,
        At::Thing => return Link::Modified,
        At::Link(resolved) => resolved,
    };
    if resolved == source {
        return Link::Healthy;
    }
    let of_a_release = releases.iter().any(|root| {
        let Some(below) = resolved
            .strip_prefix(root.trim_end_matches('/'))
            .and_then(|rest| rest.strip_prefix('/'))
        else {
            return false;
        };
        let parts: Vec<&str> = below.split('/').filter(|part| !part.is_empty()).collect();
        parts.len() == 3 && parts[1] == "skills" && parts[2] == name
    });
    if of_a_release {
        Link::Outdated
    } else {
        Link::Modified
    }
}

/// Whether Antigravity's folder is used: its configuration folder is there, and the skills
/// folder is no link that leads nowhere.
pub fn agy_usable(parent_is_dir: bool, is_link: bool, is_dir: bool) -> bool {
    parent_is_dir && (!is_link || is_dir)
}

/// The folders a change writes, given each offered folder with its resolved path: folders that
/// are one folder (a skills folder linked to another) are written once, by the first.
pub fn distinct<'a>(offered: &'a [(Root, String)]) -> Vec<Root> {
    let mut seen: Vec<&'a str> = Vec::new();
    let mut kept = Vec::new();
    for (root, resolved) in offered {
        if !seen.contains(&resolved.as_str()) {
            seen.push(resolved);
            kept.push(*root);
        }
    }
    kept
}

/// A value as Python's `str()` writes it: a text as itself, anything else as it is shown.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// Whether Python reads a value as false.
fn falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(on) => !on,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
    }
}

/// A manifest the reference cannot go through: it fails on it (an item that is no object, or
/// has no id; a list of requirements that is a number). This Host answers it as a manifest
/// that cannot be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Malformed;

/// The skills of a manifest, from its file's bytes (none: no file).
pub fn manifest(bytes: Option<&[u8]>) -> Result<Vec<Value>, Malformed> {
    let value: Value = bytes
        .and_then(|bytes| serde_json::from_slice(bytes).ok())
        .unwrap_or(Value::Null);
    match value.get("skills") {
        Some(Value::Array(skills)) => Ok(skills.clone()),
        _ => Err(Malformed),
    }
}

/// A skill's id as the reference takes it (`str(item["id"])`).
pub fn skill_id(item: &Value) -> Result<String, Malformed> {
    item.as_object()
        .and_then(|fields| fields.get("id"))
        .map(py_str)
        .ok_or(Malformed)
}

/// The requirements a skill names, each as the reference writes it (`str(expression)` over
/// what iterating `item.get("requires") or []` gives).
fn expressions(requires: Option<&Value>) -> Result<Vec<String>, Malformed> {
    let Some(requires) = requires.filter(|value| !falsy(value)) else {
        return Ok(Vec::new());
    };
    match requires {
        Value::Array(items) => Ok(items.iter().map(py_str).collect()),
        Value::String(text) => Ok(text.chars().map(String::from).collect()),
        Value::Object(fields) => Ok(fields.keys().cloned().collect()),
        _ => Err(Malformed),
    }
}

/// Whether a skill's requirements are met, and those that are not. A requirement is one or
/// more programs (`"wacli or himalaya"`); it is met when one of them is found on the path.
pub fn requirements(
    item: &Value,
    found: &mut dyn FnMut(&str) -> bool,
) -> Result<(bool, Vec<String>), Malformed> {
    let mut missing = Vec::new();
    for expression in expressions(item.get("requires"))? {
        let choices: Vec<&str> = expression
            .split(" or ")
            .map(python::strip)
            .filter(|choice| !choice.is_empty())
            .collect();
        if !choices.is_empty() && !choices.iter().any(|choice| found(choice)) {
            missing.push(expression.clone());
        }
    }
    Ok((missing.is_empty(), missing))
}

/// A skill's health, from whether its source is there, how its Claude and Codex links stand
/// (Antigravity's never decides), whether it is chosen, and whether its requirements are met.
pub fn health(
    source: bool,
    required: &[Link],
    enabled: bool,
    requirements_ok: bool,
) -> &'static str {
    let any = |state: Link| required.contains(&state);
    if !source {
        "missing"
    } else if any(Link::Modified) {
        "modified"
    } else if any(Link::Outdated) {
        "outdated"
    } else if !enabled && any(Link::Healthy) {
        "modified"
    } else if !enabled {
        "inactive"
    } else if any(Link::Missing) {
        "missing"
    } else if !requirements_ok {
        "dependency-missing"
    } else {
        "healthy"
    }
}

/// What the Host found of one skill.
pub struct Facts {
    /// Claude's and Codex's links, then Antigravity's where it is used.
    pub links: Vec<(Root, Link)>,
    pub enabled: bool,
    /// Whether the release has the skill's `SKILL.md`.
    pub source: bool,
    pub requirements: (bool, Vec<String>),
}

/// A skill's row in `GET /managed-skills`: its manifest entry with what was found.
pub fn row(item: &Value, facts: &Facts) -> Value {
    let mut row = item.as_object().cloned().unwrap_or_default();
    let required: Vec<Link> = facts
        .links
        .iter()
        .filter(|(root, _)| !root.optional())
        .map(|(_, link)| *link)
        .collect();
    let (requirements_ok, missing) = &facts.requirements;
    let links: Map<String, Value> = facts
        .links
        .iter()
        .map(|(root, link)| (root.name().to_owned(), json!(link.as_str())))
        .collect();
    row.insert("enabled".into(), json!(facts.enabled));
    row.insert(
        "optional".into(),
        json!(item.get("pack") != Some(&json!("core"))),
    );
    row.insert(
        "health".into(),
        json!(health(
            facts.source,
            &required,
            facts.enabled,
            *requirements_ok
        )),
    );
    row.insert("links".into(), Value::Object(links));
    row.insert("requirements_ok".into(), json!(requirements_ok));
    row.insert("missing_requirements".into(), json!(missing));
    Value::Object(row)
}

/// What `POST /managed-skills` asks: the skill and whether it is to be on. `None` for the
/// body: it is no JSON object.
pub fn asked(body: Option<&Value>) -> Result<(String, bool), &'static str> {
    let Some(fields) = body.and_then(Value::as_object) else {
        return Err(BAD_JSON);
    };
    let skill = fields
        .get("skill_id")
        .filter(|value| !falsy(value))
        .map(py_str)
        .unwrap_or_default();
    let skill = python::strip(&skill);
    match fields.get("enabled") {
        Some(Value::Bool(enabled)) if !skill.is_empty() => Ok((skill.to_owned(), *enabled)),
        _ => Err(REQUIRED),
    }
}

/// Why a change is refused, in the reference's order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    Unknown(String),
    CoreStays,
    SourceMissing(String),
    /// Something that is not Clarp's is at this folder's place: it is preserved.
    Preserving(Root),
}

impl Refusal {
    /// In the reference's words; `place` gives the path of a skill's place in a folder.
    pub fn words(&self, place: &dyn Fn(Root) -> String) -> String {
        match self {
            Self::Unknown(skill) => format!("unknown Clarp skill: {skill}"),
            Self::CoreStays => "core Clarp skills cannot be disabled".to_owned(),
            Self::SourceMissing(skill) => format!("skill source is missing: {skill}"),
            Self::Preserving(root) => format!("preserving non-Clarp skill at {}", place(*root)),
        }
    }
}

/// What is done at a skill's place in one folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Act {
    /// It is made (or made again) a link to this release's copy.
    Link,
    /// The link is taken away.
    Unlink,
    Leave,
}

/// The manifest entry a change names: the last of that id, as the reference's table keeps it.
pub fn entry<'a>(items: &'a [Value], skill: &str) -> Result<Option<&'a Value>, Malformed> {
    let mut found = None;
    for item in items {
        if skill_id(item)? == skill {
            found = Some(item);
        }
    }
    Ok(found)
}

/// What a change does in each folder it writes (`roots`: the distinct folders with how the
/// skill's place stands in each), or why it is refused.
pub fn change(
    item: Option<&Value>,
    skill: &str,
    enabled: bool,
    source: bool,
    roots: &[(Root, Link)],
) -> Result<Vec<(Root, Act)>, Refusal> {
    let Some(item) = item else {
        return Err(Refusal::Unknown(skill.to_owned()));
    };
    if !enabled && item.get("pack") == Some(&json!("core")) {
        return Err(Refusal::CoreStays);
    }
    if !source {
        return Err(Refusal::SourceMissing(skill.to_owned()));
    }
    let skipped = |root: Root, link: Link| root.optional() && link == Link::Modified;
    let conflict = roots
        .iter()
        .find(|(root, link)| *link == Link::Modified && !skipped(*root, *link));
    if let (true, Some((root, _))) = (enabled, conflict) {
        return Err(Refusal::Preserving(*root));
    }
    Ok(roots
        .iter()
        .map(|(root, link)| {
            let act = if skipped(*root, *link) {
                Act::Leave
            } else if enabled {
                Act::Link
            } else if matches!(link, Link::Healthy | Link::Outdated) {
                Act::Unlink
            } else {
                Act::Leave
            };
            (*root, act)
        })
        .collect())
}

/// The chosen list after a change: without doubles, sorted.
pub fn chosen_after(chosen: &[String], skill: &str, enabled: bool) -> Vec<String> {
    let mut after: Vec<String> = chosen.iter().filter(|id| *id != skill).cloned().collect();
    if enabled {
        after.push(skill.to_owned());
    }
    after.sort();
    after.dedup();
    after
}

/// What a change answers: the manifest entry, switched as asked.
pub fn changed(item: &Value, enabled: bool) -> Value {
    let mut told = item.as_object().cloned().unwrap_or_default();
    told.insert("enabled".into(), json!(enabled));
    Value::Object(told)
}

/// The first chosen list of a Host that keeps the tools: the other Host's (its `install.json`,
/// `skills`), where it has one; else the release's defaults, as the Python Host's setup chooses
/// them: every core skill, and every skill of the artifacts pack that is not off by default.
pub fn first_chosen(other: Option<&Value>, items: &[Value]) -> Result<Vec<String>, Malformed> {
    if let Some(Value::Array(listed)) = other.and_then(|state| state.get("skills")) {
        let mut chosen: Vec<String> = listed
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        chosen.sort();
        chosen.dedup();
        return Ok(chosen);
    }
    let mut chosen = Vec::new();
    for item in items {
        let pack = item.get("pack").and_then(Value::as_str).unwrap_or("");
        let on = item
            .get("default_enabled")
            .is_none_or(|value| !falsy(value));
        if pack == "core" || (pack == "artifacts" && on) {
            chosen.push(skill_id(item)?);
        }
    }
    chosen.sort();
    chosen.dedup();
    Ok(chosen)
}

/// The chosen list at a start: core skills a newer release brought are chosen too, as the
/// Python Host's `clarp-admin skills repair-links` enrols them after an update.
pub fn enrolled(chosen: &[String], items: &[Value]) -> Result<Vec<String>, Malformed> {
    let mut after = chosen.to_vec();
    for item in items {
        if item.get("pack") == Some(&json!("core")) {
            after.push(skill_id(item)?);
        }
    }
    after.sort();
    after.dedup();
    Ok(after)
}

/// What a start does at a chosen skill's place in one folder: a skill this release has is
/// linked where nothing is or another release's link is, and left where it is linked already
/// or something not Clarp's is there; a chosen skill this release does not have loses the
/// links other releases left. No other place is touched.
pub fn at_start(link: Link, in_release: bool) -> Act {
    match (in_release, link) {
        (true, Link::Missing | Link::Outdated) => Act::Link,
        (false, Link::Outdated) => Act::Unlink,
        _ => Act::Leave,
    }
}
