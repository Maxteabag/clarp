#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The skills kept installed for the tools: the kernel's rules of how a link in a tool's folder reads, a skill's
health and row, what a change does in each folder and to the chosen list, how a change is asked, the first chosen
list and what a start links; and how the Host finds what is on the disk, keeps the list, answers the two routes,
links at its start only where it keeps the machine's tools, and how the install tool ships the tree.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'managed_skills_vectors'],
    'host': ['test', '--test', 'host', '--', 'managed_skills::'],
    'install': ['test', '--test', 'host', '--', 'managed_skills::the_install_tool'],
}
K = 'clarp-kernel/src/managed_skills.rs'
H = 'clarp-host/src/managed_skills.rs'
E = 'clarp-host/src/edge.rs'
C = 'clarp-host/src/config.rs'
M = 'clarp-host/src/main.rs'
I = '../tools/install-host'
# NOT PLANTED, each with its reason:
#   that an empty text reads as false (`Value::String(text) => text.is_empty()` in `falsy`): an empty text gives
#     the same either way, as a skill's id (stripped, it is empty) and as its requirements (it has no signs).
#   that the folders a change writes are resolved before they are compared (`resolve(self.folder(root))` in
#     `written_roots`): a folder that is another's, written twice, ends with the same link as written once.
#   that a skill's source is resolved before it is compared (`resolve(&self.source(skill))` in `link`): the tree is
#     resolved when the Host starts, and a release's tree holds no links of its own.
#   that a name with a slash is that file (`if name.contains('/')` in `on_path`): a directory of the path joined
#     with an absolute name is that name, so only a relative name with a slash differs, and no skill names one.
#   what is put back when a change cannot be kept (`Undo`): it runs only when a write of the disk or of the store
#     fails after another succeeded, which a Host test does not bring about.
#   that a link left beside a place by a change that was cut off is taken away first (`if beside.is_symlink()` in
#     `put_link`): there is one only after a Host was killed in the middle of a change.
PLANTS = [
    # How a link in a tool's folder reads.
    (K, 'vectors', 'nothing there reads modified', '        At::Nothing => return Link::Missing,', '        At::Nothing => return Link::Modified,'),
    (K, 'vectors', 'a folder there reads missing', '        At::Thing => return Link::Modified,', '        At::Thing => return Link::Missing,'),
    (K, 'vectors', 'a link to this release is never healthy', '    if resolved == source {', '    if resolved == source && resolved.is_empty() {'),
    (K, 'vectors', 'a link to this release reads outdated', '        return Link::Healthy;', '        return Link::Outdated;'),
    (K, 'vectors', 'a link deeper into a release reads outdated', '        parts.len() == 3 && parts[1] == "skills" && parts[2] == name', '        parts.len() >= 3 && parts[1] == "skills" && parts[2] == name'),
    (K, 'vectors', 'any folder of a release reads outdated', '        parts.len() == 3 && parts[1] == "skills" && parts[2] == name', '        parts.len() == 3 && !parts[1].is_empty() && parts[2] == name'),
    (K, 'vectors', 'another skill of a release reads outdated', '        parts.len() == 3 && parts[1] == "skills" && parts[2] == name', '        parts.len() == 3 && parts[1] == "skills" && !parts[2].is_empty()'),
    (K, 'vectors', 'a folder beginning with the releases folder\'s name is one', "            .and_then(|rest| rest.strip_prefix('/'))", '            .map(|rest| rest)'),
    (K, 'vectors', 'only the first folder of releases counts', '    let of_a_release = releases.iter().any(|root| {', '    let of_a_release = releases.iter().take(1).any(|root| {'),
    (K, 'vectors', 'another release\'s link reads modified', '    if of_a_release {\n        Link::Outdated', '    if of_a_release {\n        Link::Modified'),
    (K, 'vectors', 'a foreign link reads outdated', '    } else {\n        Link::Modified\n    }\n}', '    } else {\n        Link::Outdated\n    }\n}'),
    (K, 'vectors', 'the word of an outdated link', '            Self::Outdated => "outdated",', '            Self::Outdated => "old",'),
    (K, 'vectors', 'the word of a modified link', '            Self::Modified => "modified",', '            Self::Modified => "changed",'),
    # Which folders are used and written.
    (K, 'vectors', 'Antigravity is used without its configuration folder', '    parent_is_dir && (!is_link || is_dir)', '    parent_is_dir || (!is_link || is_dir)'),
    (K, 'vectors', 'a dangling link is a usable folder', '    parent_is_dir && (!is_link || is_dir)', '    parent_is_dir && (is_link || is_dir)'),
    (K, 'vectors', 'a linked folder is not usable', '    parent_is_dir && (!is_link || is_dir)', '    parent_is_dir && !is_link'),
    (K, 'vectors', 'a folder that is another\'s is written again', '        if !seen.contains(&resolved.as_str()) {', '        if !seen.is_empty() || seen.is_empty() {'),
    (K, 'vectors', 'what was seen is forgotten', '            seen.push(resolved);', '            seen.push("");'),
    (K, 'vectors', 'Antigravity\'s folder decides', '        self == Self::Agy', '        self == Self::Codex'),
    (K, 'host', 'Antigravity\'s word', '            Self::Agy => "agy",', '            Self::Agy => "antigravity",'),
    # What Python reads as false and how it writes a value.
    (K, 'vectors', 'null is true', '        Value::Null => true,', '        Value::Null => false,'),
    (K, 'vectors', 'false is true', '        Value::Bool(on) => !on,', '        Value::Bool(on) => *on,'),
    (K, 'vectors', 'zero is true', '        Value::Number(number) => number.as_f64() == Some(0.0),', '        Value::Number(number) => number.as_f64() == Some(1.0),'),
    (K, 'vectors', 'an empty list is true', '        Value::Array(items) => items.is_empty(),', '        Value::Array(items) => !items.is_empty() && false,'),
    (K, 'vectors', 'an empty object is true', '        Value::Object(fields) => fields.is_empty(),', '        Value::Object(fields) => !fields.is_empty() && false,'),
    (K, 'vectors', 'a value is written as JSON', '        other => python::repr(other),', '        other => other.to_string(),'),
    (K, 'vectors', 'a text is written with quotes', '        Value::String(text) => text.clone(),', '        Value::String(text) => python::text(text),'),
    # The manifest and the ids.
    (K, 'vectors', 'a manifest without a list is empty', '        Some(Value::Array(skills)) => Ok(skills.clone()),\n        _ => Err(Malformed),', '        Some(Value::Array(skills)) => Ok(skills.clone()),\n        _ => Ok(Vec::new()),'),
    (K, 'vectors', 'an id is taken only as a text', '        .map(py_str)\n        .ok_or(Malformed)', '        .map(|id| id.as_str().unwrap_or("").to_owned())\n        .ok_or(Malformed)'),
    (K, 'vectors', 'an item without an id has an empty one', '        .map(py_str)\n        .ok_or(Malformed)', '        .map(py_str)\n        .or(Some(String::new()))\n        .ok_or(Malformed)'),
    # Requirements.
    (K, 'vectors', 'a false list of requirements is iterated', '    let Some(requires) = requires.filter(|value| !falsy(value)) else {', '    let Some(requires) = requires.filter(|_| true) else {'),
    (K, 'vectors', 'a text of requirements is one requirement', '        Value::String(text) => Ok(text.chars().map(String::from).collect()),', '        Value::String(text) => Ok(vec![text.clone()]),'),
    (K, 'vectors', 'an object of requirements is malformed', '        Value::Object(fields) => Ok(fields.keys().cloned().collect()),', '        Value::Object(_) => Err(Malformed),'),
    (K, 'vectors', 'a number of requirements is none', '        Value::Object(fields) => Ok(fields.keys().cloned().collect()),\n        _ => Err(Malformed),', '        Value::Object(fields) => Ok(fields.keys().cloned().collect()),\n        _ => Ok(Vec::new()),'),
    (K, 'vectors', 'choices are split on another word', '            .split(" or ")', '            .split(" OR ")'),
    (K, 'vectors', 'choices keep their blanks', '            .map(python::strip)', '            .map(|choice| choice)'),
    (K, 'vectors', 'an empty choice is looked up', '            .filter(|choice| !choice.is_empty())', '            .filter(|_| true)'),
    (K, 'vectors', 'a requirement of no choices is unmet', '        if !choices.is_empty() && !choices.iter().any(|choice| found(choice)) {', '        if !choices.iter().any(|choice| found(choice)) {'),
    (K, 'vectors', 'every choice must be found', '        if !choices.is_empty() && !choices.iter().any(|choice| found(choice)) {', '        if !choices.is_empty() && !choices.iter().all(|choice| found(choice)) {'),
    (K, 'vectors', 'an unmet requirement is told as its choices', '            missing.push(expression.clone());', '            missing.push(choices.join(" or "));'),
    (K, 'vectors', 'requirements are always met', '    Ok((missing.is_empty(), missing))', '    Ok((true, missing))'),
    # Health.
    (K, 'vectors', 'a skill without its source is inactive', '    if !source {\n        "missing"', '    if !source {\n        "inactive"'),
    (K, 'vectors', 'a foreign link matters only when chosen', '    } else if any(Link::Modified) {', '    } else if any(Link::Modified) && enabled {'),
    (K, 'vectors', 'a foreign link reads as outdated health', '    } else if any(Link::Modified) {\n        "modified"', '    } else if any(Link::Modified) {\n        "outdated"'),
    (K, 'vectors', 'an outdated link matters only when chosen', '    } else if any(Link::Outdated) {', '    } else if any(Link::Outdated) && enabled {'),
    (K, 'vectors', 'a link of a skill not chosen is not modified', '    } else if !enabled && any(Link::Healthy) {', '    } else if !enabled && any(Link::Missing) {'),
    (K, 'vectors', 'a skill not chosen reads missing', '    } else if !enabled {\n        "inactive"', '    } else if !enabled {\n        "missing"'),
    (K, 'vectors', 'a chosen skill without a link reads inactive', '    } else if any(Link::Missing) {\n        "missing"', '    } else if any(Link::Missing) {\n        "inactive"'),
    (K, 'vectors', 'requirements never decide', '    } else if !requirements_ok {', '    } else if requirements_ok && !requirements_ok {'),
    (K, 'vectors', 'a skill in order reads unmet', '    } else {\n        "healthy"\n    }', '    } else {\n        "dependency-missing"\n    }'),
    # The row.
    (K, 'vectors', 'Antigravity\'s link decides health', '        .filter(|(root, _)| !root.optional())', '        .filter(|_| true)'),
    (K, 'vectors', 'the row loses the entry\'s own fields', '    let mut row = item.as_object().cloned().unwrap_or_default();', '    let mut row = Map::new();'),
    (K, 'vectors', 'chosen is told the other way', '    row.insert("enabled".into(), json!(facts.enabled));', '    row.insert("enabled".into(), json!(!facts.enabled));'),
    (K, 'vectors', 'only the optional pack is optional', '        json!(item.get("pack") != Some(&json!("core"))),', '        json!(item.get("pack") == Some(&json!("optional"))),'),
    (K, 'vectors', 'the links are named otherwise', '        .map(|(root, link)| (root.name().to_owned(), json!(link.as_str())))', '        .map(|(root, link)| (root.name().to_uppercase(), json!(link.as_str())))'),
    (K, 'vectors', 'requirements are told met', '    row.insert("requirements_ok".into(), json!(requirements_ok));', '    row.insert("requirements_ok".into(), json!(true));'),
    (K, 'vectors', 'no unmet requirement is told', '    row.insert("missing_requirements".into(), json!(missing));', '    row.insert("missing_requirements".into(), json!([]));'),
    # How a change is asked.
    (K, 'vectors', 'a body that is no object lacks fields', '        return Err(BAD_JSON);', '        return Err(REQUIRED);'),
    (K, 'vectors', 'a false id is written', '        .get("skill_id")\n        .filter(|value| !falsy(value))', '        .get("skill_id")\n        .filter(|_| true)'),
    (K, 'vectors', 'an id keeps its blanks', '    let skill = python::strip(&skill);', '    let skill = skill.as_str();'),
    (K, 'vectors', 'an empty id is taken', '        Some(Value::Bool(enabled)) if !skill.is_empty() => Ok((skill.to_owned(), *enabled)),', '        Some(Value::Bool(enabled)) => Ok((skill.to_owned(), *enabled)),'),
    (K, 'vectors', 'every change switches on', '        Some(Value::Bool(enabled)) if !skill.is_empty() => Ok((skill.to_owned(), *enabled)),', '        Some(Value::Bool(_)) if !skill.is_empty() => Ok((skill.to_owned(), true)),'),
    (K, 'vectors', 'the words of a missing field', 'pub const REQUIRED: &str = "skill_id and boolean enabled are required";', 'pub const REQUIRED: &str = "skill_id and enabled are required";'),
    (K, 'host', 'the words of a body that is no object', 'pub const BAD_JSON: &str = "bad json";', 'pub const BAD_JSON: &str = "bad JSON";'),
    (K, 'vectors', 'the words of an unreadable manifest', 'pub const UNAVAILABLE: &str = "Clarp skill manifest is unavailable";', 'pub const UNAVAILABLE: &str = "Clarp skill manifest is missing";'),
    # Why a change is refused.
    (K, 'vectors', 'the words of an unknown skill', '            Self::Unknown(skill) => format!("unknown Clarp skill: {skill}"),', '            Self::Unknown(skill) => format!("unknown skill: {skill}"),'),
    (K, 'vectors', 'the words of a core skill', '            Self::CoreStays => "core Clarp skills cannot be disabled".to_owned(),', '            Self::CoreStays => "core Clarp skills stay".to_owned(),'),
    (K, 'vectors', 'the words of a missing source', '            Self::SourceMissing(skill) => format!("skill source is missing: {skill}"),', '            Self::SourceMissing(skill) => format!("skill source missing: {skill}"),'),
    (K, 'vectors', 'the words of a preserved skill', '            Self::Preserving(root) => format!("preserving non-Clarp skill at {}", place(*root)),', '            Self::Preserving(root) => format!("preserving non-Clarp skill in {}", root.name()),'),
    (K, 'vectors', 'the first of two entries is taken', '            found = Some(item);', '            found = found.or(Some(item));'),
    (K, 'vectors', 'an unknown skill is a core one', '        return Err(Refusal::Unknown(skill.to_owned()));', '        return Err(Refusal::CoreStays);'),
    (K, 'vectors', 'a core skill cannot be switched on either', '    if !enabled && item.get("pack") == Some(&json!("core")) {', '    if item.get("pack") == Some(&json!("core")) {'),
    (K, 'vectors', 'a missing source is not refused', '    if !source {\n        return Err(Refusal::SourceMissing(skill.to_owned()));', '    if source && !source {\n        return Err(Refusal::SourceMissing(skill.to_owned()));'),
    (K, 'vectors', 'Antigravity\'s own skill holds the others back', '    let skipped = |root: Root, link: Link| root.optional() && link == Link::Modified;', '    let skipped = |root: Root, link: Link| root.optional() && link == Link::Modified && false;'),
    (K, 'vectors', 'nothing is preserved', '        .find(|(root, link)| *link == Link::Modified && !skipped(*root, *link));', '        .find(|(root, link)| *link == Link::Missing && !skipped(*root, *link));'),
    (K, 'vectors', 'switching off is refused by a foreign skill', '    if let (true, Some((root, _))) = (enabled, conflict) {', '    if let (_, Some((root, _))) = (enabled, conflict) {'),
    # What a change does.
    (K, 'vectors', 'Antigravity\'s own skill is replaced', '            let act = if skipped(*root, *link) {\n                Act::Leave', '            let act = if skipped(*root, *link) {\n                Act::Link'),
    (K, 'vectors', 'switching on links nothing', '            } else if enabled {\n                Act::Link', '            } else if enabled {\n                Act::Leave'),
    (K, 'vectors', 'switching off keeps an outdated link', '            } else if matches!(link, Link::Healthy | Link::Outdated) {', '            } else if matches!(link, Link::Healthy) {'),
    (K, 'vectors', 'switching off keeps a healthy link', '            } else if matches!(link, Link::Healthy | Link::Outdated) {', '            } else if matches!(link, Link::Outdated) {'),
    (K, 'vectors', 'switching off keeps the skill chosen', '    let mut after: Vec<String> = chosen.iter().filter(|id| *id != skill).cloned().collect();', '    let mut after: Vec<String> = chosen.to_vec();'),
    (K, 'vectors', 'switching on chooses nothing', '    if enabled {\n        after.push(skill.to_owned());', '    if !enabled {\n        after.push(skill.to_owned());'),
    (K, 'vectors', 'the chosen list is kept unsorted', '        after.push(skill.to_owned());\n    }\n    after.sort();', '        after.push(skill.to_owned());\n    }'),
    (K, 'vectors', 'the answer is always switched on', '    told.insert("enabled".into(), json!(enabled));', '    told.insert("enabled".into(), json!(true));'),
    # The first chosen list and a start.
    (K, 'vectors', 'the other Host\'s list is not taken', '    if let Some(Value::Array(listed)) = other.and_then(|state| state.get("skills")) {', '    if let Some(Value::Array(listed)) = other.and_then(|state| state.get("chosen")) {'),
    (K, 'vectors', 'the other Host\'s list keeps its doubles', '        chosen.dedup();\n        return Ok(chosen);', '        return Ok(chosen);'),
    (K, 'vectors', 'every artifact skill is chosen by default', '        if pack == "core" || (pack == "artifacts" && on) {', '        if pack == "core" || pack == "artifacts" {'),
    (K, 'vectors', 'an artifact skill off by zero is chosen', '            .is_none_or(|value| !falsy(value));', '            .is_none_or(|value| value != &json!(false));'),
    (K, 'vectors', 'core skills are not chosen by default', '        if pack == "core" || (pack == "artifacts" && on) {', '        if pack == "artifacts" && on {'),
    (K, 'vectors', 'a start enrols no core skill', '        if item.get("pack") == Some(&json!("core")) {\n            after.push(skill_id(item)?);', '        if item.get("pack") == Some(&json!("none")) {\n            after.push(skill_id(item)?);'),
    (K, 'vectors', 'a start leaves another release\'s link', '        (true, Link::Missing | Link::Outdated) => Act::Link,', '        (true, Link::Missing) => Act::Link,'),
    (K, 'vectors', 'a start replaces what is not Clarp\'s', '        (true, Link::Missing | Link::Outdated) => Act::Link,', '        (true, Link::Missing | Link::Outdated | Link::Modified) => Act::Link,'),
    (K, 'vectors', 'a start keeps a gone skill\'s link', '        (false, Link::Outdated) => Act::Unlink,', '        (false, Link::Outdated) => Act::Leave,'),
    # The refusals of a Host that does not keep the tools.
    (K, 'host', 'the word of a Host that does not keep the tools', 'pub const KEPT_ELSEWHERE: &str = "skills_kept_elsewhere";', 'pub const KEPT_ELSEWHERE: &str = "kept_elsewhere";'),
    (K, 'host', 'the sentence of a Host that does not keep the tools', 'pub const KEPT_ELSEWHERE_WHY: &str = "This Host does not keep this machine\'s tools; \\', 'pub const KEPT_ELSEWHERE_WHY: &str = "This Host does not keep the tools; \\'),
    (K, 'host', 'the word of a Host without a tree', 'pub const NOT_SHIPPED: &str = "skills_not_shipped";', 'pub const NOT_SHIPPED: &str = "not_shipped";'),
    # The Host: whether it keeps the tools.
    (H, 'host', 'a Host with no mode keeps the tools', '        if config.accounts != Accounts::Select {\n            return None;', '        if config.accounts == Accounts::Follow {\n            return None;'),
    (H, 'host', 'a Host that follows keeps the tools and writes at its start', '        if config.accounts != Accounts::Select {\n            return None;', '        if config.accounts == Accounts::None {\n            return None;'),
    (H, 'host', 'a change on a Host with no mode is not refused as kept elsewhere', '    if app.config.accounts != Accounts::Select {\n        return refused(', '    if app.config.accounts == Accounts::Follow {\n        return refused('),
    (H, 'host', 'a change on a following Host is not refused as kept elsewhere', '    if app.config.accounts != Accounts::Select {\n        return refused(', '    if app.config.accounts == Accounts::None {\n        return refused('),
    (H, 'host', 'a refusal is forbidden', '    (StatusCode::CONFLICT, Json(told)).into_response()', '    (StatusCode::FORBIDDEN, Json(told)).into_response()'),
    (H, 'host', 'a refusal is told without its word', '    let told = json!({ "error": why, "code": code });', '    let told = json!({ "error": why, "code": why });'),
    (H, 'host', 'a Host without a tree refuses as kept elsewhere', '        return refused(rules::NOT_SHIPPED, rules::NOT_SHIPPED_WHY);', '        return refused(rules::KEPT_ELSEWHERE, rules::NOT_SHIPPED_WHY);'),
    # The Host: where things are.
    (H, 'host', 'this Host\'s own older releases are foreign', '        if let Some(own) = tree.parent().and_then(Path::parent) {', '        if let Some(own) = tree.parent() {'),
    (H, 'host', 'the other Host\'s releases are foreign', '            releases.push(text(&resolve(other)));', '            let _ = other;'),
    (H, 'host', 'Codex\'s folder is elsewhere', '        let codex_home = var("CODEX_HOME").unwrap_or_else(|| home().join(".codex"));', '        let codex_home = var("CODEX_HOME").unwrap_or_else(|| home().join(".codex-x"));'),
    (H, 'host', 'Claude\'s folder is elsewhere', '            claude: var("CLARP_CLAUDE_SKILLS").unwrap_or_else(|| home().join(".claude/skills")),', '            claude: var("CLARP_CLAUDE_SKILLS").unwrap_or_else(|| home().join(".claude/skill")),'),
    (H, 'host', 'Antigravity\'s folder is elsewhere', '            agy: var("CLARP_AGY_SKILLS").unwrap_or_else(|| home().join(".gemini/config/skills")),', '            agy: var("CLARP_AGY_SKILLS").unwrap_or_else(|| home().join(".gemini/skills")),'),
    (H, 'host', 'a source is there without its SKILL.md', '        self.source(skill).join("SKILL.md").is_file()', '        self.source(skill).join("SKILL.md").is_file() || !skill.is_empty()'),
    (H, 'host', 'Antigravity is used without its configuration folder', '        let parent_is_dir = self.agy.parent().is_some_and(Path::is_dir);', '        let parent_is_dir = true;'),
    (H, 'host', 'Antigravity is never used', '        rules::agy_usable(parent_is_dir, self.agy.is_symlink(), self.agy.is_dir())', '        rules::agy_usable(false, self.agy.is_symlink(), self.agy.is_dir())'),
    (H, 'host', 'nothing there reads as a folder', '            Err(_) => At::Nothing,', '            Err(_) => At::Thing,'),
    (H, 'host', 'a link reads as a folder', '            Ok(found) if found.file_type().is_symlink() => {', '            Ok(found) if found.file_type().is_symlink() && false => {'),
    (H, 'host', 'every skill is chosen', '                enabled: chosen.contains(&skill),', '                enabled: !chosen.is_empty(),'),
    (H, 'host', 'every source is there', '                source: self.has_source(&skill),', '                source: true,'),
    (H, 'host', 'a file that does not run is a program', '            .is_ok_and(|found| !found.is_dir() && found.permissions().mode() & 0o111 != 0)', '            .is_ok_and(|found| !found.is_dir())'),
    (H, 'host', 'a folder is a program', '            .is_ok_and(|found| !found.is_dir() && found.permissions().mode() & 0o111 != 0)', '            .is_ok_and(|found| found.permissions().mode() & 0o111 != 0)'),
    # The Host: the chosen list.
    (H, 'host', 'the kept list is not read', '    Some(serde_json::from_str(&kept).unwrap_or_default())', '    Some(serde_json::from_str::<Vec<String>>(&kept).map(|_| Vec::new()).unwrap_or_default())'),
    (H, 'host', 'the list is kept under another key', '        .set(vec![(rules::CHOSEN_SETTING.to_owned(), kept)])', '        .set(vec![("managed_skills.other".to_owned(), kept)])'),
    (H, 'host', 'a change forgets the others chosen', '    let after = rules::chosen_after(&before, &skill, enabled);', '    let after = rules::chosen_after(&[], &skill, enabled);'),
    (H, 'host', 'a change is not kept', '    if let Err(error) = keep_chosen(app, &after).await {', '    if let Err(error) = Ok::<(), ApiError>(()) {'),
    # The Host: answers.
    (H, 'host', 'an empty body is no JSON', '        Some(json!({}))\n    } else {', '        None\n    } else {'),
    (H, 'host', 'a body that lacks fields is a conflict', '        Err(why) => return ApiError::new(StatusCode::BAD_REQUEST, why).into_response(),', '        Err(why) => return ApiError::new(StatusCode::CONFLICT, why).into_response(),'),
    (H, 'host', 'a refused change is a bad request', '                return Ok(Err(ApiError::new(StatusCode::CONFLICT, words)));', '                return Ok(Err(ApiError::new(StatusCode::BAD_REQUEST, words)));'),
    (H, 'host', 'a preserved skill is named by its folder', '                let words = refusal.words(&|root| text(&shelf.place(root, &skill)));', '                let words = refusal.words(&|root| text(shelf.folder(root)));'),
    (H, 'host', 'a status of no Host is unavailable', '        return Ok(Json(json!({ "skills": [] })));', '        return Err(unavailable());'),
    # The Host: writing.
    (H, 'host', 'what is left is written too', '            if *act == Act::Leave {\n                continue;', '            if *act == Act::Unlink {\n                continue;'),
    (H, 'host', 'a link leads to the tree', '                    .and_then(|()| put_link(&place, &self.source(skill))),', '                    .and_then(|()| put_link(&place, &self.tree)),'),
    (H, 'host', 'a link switched off stays', '                _ if place.is_symlink() => std::fs::remove_file(&place),', '                _ if place.is_symlink() => Ok(()),'),
    # The Host: its start.
    (H, 'host', 'a start forgets the kept list', '            Some(kept) => kept,', '            Some(_) => Vec::new(),'),
    (H, 'host', 'a start does not read the other Host\'s list', '                    .and_then(|file| std::fs::read(file).ok())', '                    .and_then(|_| None::<Vec<u8>>)'),
    (H, 'host', 'a start enrols no core skill', '        let chosen = rules::enrolled(&first, &items)?;', '        let chosen = first;'),
    (H, 'host', 'a gone skill is linked at a start', '            let in_release = rules::entry(&items, skill)?.is_some() && shelf.has_source(skill);', '            let in_release = true;'),
    (H, 'host', 'the first list is not kept', '            if was.as_ref() == Some(&chosen) {\n                return;', '            if was.as_ref() != Some(&chosen) || true {\n                return;'),
    (H, 'host', 'a Host keeps skills without a tree', '    config.accounts == Accounts::Select && config.skills.is_some()', '    config.accounts == Accounts::Select'),
    (H, 'host', 'a Host keeps skills in every mode', '    config.accounts == Accounts::Select && config.skills.is_some()', '    config.skills.is_some()'),
    (M, 'host', 'a start links nothing', '    clarp_host::managed_skills::at_start(&app).await;', '    let _ = &app;'),
    # The feature and the words of the command line.
    (E, 'host', 'every Host lists the feature', '            && (*feature != "managed_skills" || skills)', '            && (*feature != "managed_skills" || true)'),
    (E, 'host', 'the feature\'s number', '    ("managed_skills", 1),', '    ("managed_skills", 2),'),
    (C, 'host', 'the tree is not named', '                "--skills" => skills = Some(PathBuf::from(value("a directory")?)),', '                "--skills" => skills = Some(PathBuf::from(value("a directory")?)).filter(|_| false),'),
    (C, 'host', 'the other Host\'s releases are not named', '                    skills_other_releases = Some(PathBuf::from(value("a directory")?));', '                    skills_other_releases = Some(PathBuf::from(value("a directory")?)).filter(|_| false);'),
    (C, 'host', 'the other Host\'s list is not named', '                    skills_chosen_before = Some(PathBuf::from(value("a file")?));', '                    skills_chosen_before = Some(PathBuf::from(value("a file")?)).filter(|_| false);'),
    # The install tool.
    (I, 'install', 'the tree is not named to the Host', '    skills=" --skills $release/skills"', '    skills=""'),
    (I, 'install', 'the other Host\'s releases are not named', '    [ -d "$python_share/releases" ] && skills="$skills --skills-other-releases $python_share/releases"', '    true'),
    (I, 'install', 'the other Host\'s list is not named', '    [ -f "$python_chosen" ] && skills="$skills --skills-chosen-before $python_chosen"', '    true'),
    (I, 'install', 'the tree is copied under one name for every version', '    release="$share/releases/$version"', '    release="$share/releases/current"'),
    (I, 'install', 'the version before is removed', '        if [ "$old" != "$version" ] && [ "$old" != "$previous" ]; then', '        if [ "$old" != "$version" ]; then'),
    (I, 'install', 'older versions stay', '        if [ "$old" != "$version" ] && [ "$old" != "$previous" ]; then', '        if false; then'),
    # The install tool's gate: select only where no Python Host runs.
    (I, 'install', 'a running Python Host is not seen', '            *) return 0 ;;', '            *) return 1 ;;'),
    (I, 'install', 'the runtime unit is not looked at', '    for unit in clarp.service clarp-runtime.service; do', '    for unit in clarp.service; do'),
    (I, 'install', 'a stopped Host that failed is seen as running', "            inactive|failed|unknown|'') ;;", "            inactive|unknown|'') ;;"),
    (I, 'install', 'select beside a running Host without a selector is taken', 'if [ "$accounts" = select ] && python_runs; then', 'if [ "$accounts" = select ] && python_runs && grep -q "_account_switch_command" "$python_config" 2>/dev/null; then'),
    (I, 'install', 'the refusal says other words', '    echo "  keeps them. Stop that Host first, or use --accounts follow beside it." >&2', '    echo "  keeps them. Use --accounts follow." >&2'),
    (I, 'install', 'the refusal goes on and writes', '    echo "  keeps them. Stop that Host first, or use --accounts follow beside it." >&2\n    exit 2', '    echo "  keeps them. Stop that Host first, or use --accounts follow beside it." >&2\n    true'),
    (I, 'install', 'a tree named without a manifest is taken', '[ "$skills_named" = 1 ] && [ ! -f "$skills_from/manifest.json" ]; then', '[ "$skills_named" = 2 ] && [ ! -f "$skills_from/manifest.json" ]; then'),
    (I, 'install', 'the tree named is not the one shipped', '        --skills-from) skills_from="$2"; skills_named=1; shift 2 ;;', '        --skills-from) skills_named=1; shift 2 ;;'),
    (I, 'install', 'the tree is copied without its skills', '        cp -R "$skills_from" "$release/.skills.new"', '        mkdir -p "$release/.skills.new" && cp "$skills_from/manifest.json" "$release/.skills.new"'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
