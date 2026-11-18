//! Whether a payload is a render spec this Host takes (`render_spec.validate`): the first
//! rule it fails, in the reference's order of stages, as a code and a pointer.

use std::collections::BTreeSet;

use clarp_speech::markup::strip_hidden_blocks;

use super::catalog::{Catalog, Component, Ident, TYPES};
use super::json::{self, J, Obj, Text, canonical, index, sorted, step};
use super::{OPTION_ORDER, Refusal};

/// The most bytes a payload, a spec and an initial state may have, written canonically.
pub const PAYLOAD_MOST: usize = 131_072;
pub const SPEC_MOST: usize = 32_768;
pub const STATE_MOST: usize = 4_096;
/// How many elements a spec has at least and at most, and how deep its tree may be.
pub const ELEMENTS_FEWEST: usize = 1;
pub const ELEMENTS_MOST: usize = 200;
pub const DEPTH_MOST: usize = 12;
/// How long a version and a submit label may be, in signs.
pub const VERSION_MOST: usize = 80;
pub const LABEL_MOST: usize = 40;
/// How deep the check for keys that come twice and for `on` goes.
const UNIQUE_DEEPEST: usize = 64;

/// The fields of a payload, each required.
const FIELDS: [&str; 6] = [
    "version",
    "catalog",
    "catalog_version",
    "spec",
    "submit",
    "answer_schema",
];

/// Who a check is about: the catalog's identifier rule, for the pointers it names.
pub(crate) struct Checks<'a> {
    pub(crate) catalog: &'a Catalog,
}

impl Checks<'_> {
    fn ident(&self) -> &Ident {
        &self.catalog.ident
    }

    pub(crate) fn step(&self, path: &str, key: &Text) -> String {
        step(path, key, Some(self.ident()))
    }

    fn step_str(&self, path: &str, key: &str) -> String {
        self.step(path, &Text::from(key))
    }

    pub(crate) fn canonical(&self, value: &J, path: &str) -> Result<String, Refusal> {
        canonical(value, path, Some(self.ident()))
    }

    /// An object with these keys allowed and these required (`render_spec._object`). A key
    /// that is not allowed is refused with `unknown`, the first in the order of UTF-16.
    pub(crate) fn object<'v>(
        &self,
        value: &'v J,
        path: &str,
        allowed: &dyn Fn(&Text) -> bool,
        required: &[&str],
        unknown: &'static str,
    ) -> Result<&'v Obj, Refusal> {
        let Some(fields) = value.object() else {
            return Err(Refusal::new("invalid_object", path));
        };
        if fields.duplicate {
            return Err(Refusal::new("invalid_object", path));
        }
        let extra = sorted(fields.keys().filter(|key| !allowed(key)))?;
        if let Some(key) = extra.first() {
            return Err(Refusal::new(unknown, &self.step(path, key)));
        }
        let mut missing: Vec<&str> = required
            .iter()
            .copied()
            .filter(|key| !fields.contains(key))
            .collect();
        missing.sort_by_key(|key| key.encode_utf16().collect::<Vec<u16>>());
        if let Some(key) = missing.first() {
            return Err(Refusal::new("missing_key", &self.step_str(path, key)));
        }
        Ok(fields)
    }

    /// An object with exactly these keys allowed (a closed list of names).
    pub(crate) fn closed<'v>(
        &self,
        value: &'v J,
        path: &str,
        allowed: &[&str],
        required: &[&str],
        unknown: &'static str,
    ) -> Result<&'v Obj, Refusal> {
        let allows = |key: &Text| key.as_str().is_some_and(|key| allowed.contains(&key));
        self.object(value, path, &allows, required, unknown)
    }
}

/// The texts of a list of required keys, as a schema of the catalog or a payload has them.
fn required_of(rule: &J) -> Vec<String> {
    match rule.get("required") {
        Some(J::Array(items)) => items
            .iter()
            .filter_map(|item| item.str().map(str::to_string))
            .collect(),
        _ => Vec::new(),
    }
}

/// Whether a value is of a type the schema language names (`_TYPES[kind]`).
fn of_type(value: &J, kind: &str) -> bool {
    matches!(
        (kind, value),
        ("string", J::Text(_))
            | ("boolean", J::Bool(_))
            | ("integer", J::Int(_))
            | ("object", J::Object(_))
            | ("array", J::Array(_))
    )
}

/// Two values the same, of the same type (`render_spec._same`).
fn same(one: &J, two: &J) -> bool {
    one == two
}

/// A whole number of a rule, where a rule has one.
fn bound(rule: &Obj, key: &str) -> Option<i128> {
    match rule.get(key) {
        Some(J::Int(number)) => Some(*number),
        Some(J::Bool(truth)) => Some(i128::from(*truth)),
        _ => None,
    }
}

fn length(count: usize) -> i128 {
    i128::try_from(count).unwrap_or(i128::MAX)
}

/// Whether a value fits a rule of the answer schema (`render_spec._fits`). The rule was
/// checked: its type is one of three, its bounds are whole numbers.
pub(crate) fn fits(value: &J, rule: &Obj, enum_too: bool) -> bool {
    if let Some(kind) = rule.get("type").and_then(J::str)
        && !matches!(
            (kind, value),
            ("string", J::Text(_)) | ("boolean", J::Bool(_)) | ("integer", J::Int(_))
        )
    {
        return false;
    }
    if enum_too
        && let Some(J::Array(members)) = rule.get("enum")
        && !members.iter().any(|member| same(member, value))
    {
        return false;
    }
    match value {
        J::Text(text) => {
            let long = length(text.len());
            bound(rule, "minLength").unwrap_or(0) <= long
                && long <= bound(rule, "maxLength").unwrap_or(long)
        }
        _ => true,
    }
}

impl Checks<'_> {
    /// The answer schema: an object of keys, each a string, a truth or a whole number, or a
    /// list of values (`render_spec._schema`).
    fn schema(&self, schema: &J) -> Result<(), Refusal> {
        let path = "/answer_schema";
        let fields = self.closed(
            schema,
            path,
            &["type", "properties", "required"],
            &["type", "properties"],
            "schema_keyword",
        )?;
        let props = fields.get("properties");
        let kind = fields.get("type");
        let Some(props) = props.and_then(J::object).filter(|_| kind.and_then(J::str) == Some("object"))
        else {
            return Err(Refusal::new("schema_shape", path));
        };
        let wrong_required = || Refusal::new("schema_shape", &format!("{path}/required"));
        match fields.get("required") {
            None => {}
            Some(J::Array(required)) => {
                let mut seen = BTreeSet::new();
                for key in required {
                    let Some(key) = key.str().filter(|key| self.ident().matches(key)) else {
                        return Err(wrong_required());
                    };
                    if !props.contains(key) {
                        return Err(wrong_required());
                    }
                    seen.insert(key);
                }
                if seen.len() != required.len() {
                    return Err(wrong_required());
                }
            }
            Some(_) => return Err(wrong_required()),
        }
        for key in json::ordered(props)? {
            let at = self.step(&format!("{path}/properties"), key);
            let name = key.as_str().filter(|name| self.ident().matches(name) && *name != "on");
            if name.is_none() {
                return Err(Refusal::new("schema_shape", &at));
            }
            let rule = props.at(key).unwrap_or(&J::Null);
            self.property(rule, &at)?;
        }
        Ok(())
    }

    /// One key of the answer schema.
    fn property(&self, rule: &J, at: &str) -> Result<(), Refusal> {
        let rule = self.closed(
            rule,
            at,
            &["type", "enum", "minLength", "maxLength"],
            &[],
            "schema_keyword",
        )?;
        let kind = rule.get("type");
        let named = kind.and_then(J::str);
        let listed = rule.get("enum").filter(|members| **members != J::Null);
        if (kind.is_some() && !matches!(named, Some("string" | "boolean" | "integer")))
            || (!kind.is_some_and(J::truthy) && listed.is_none())
        {
            return Err(Refusal::new("schema_shape", at));
        }
        for name in ["minLength", "maxLength"] {
            let Some(given) = rule.get(name) else {
                continue;
            };
            let whole = matches!(given, J::Int(number) if *number >= 0);
            if named != Some("string") || !whole {
                return Err(Refusal::new("schema_shape", &self.step_str(at, name)));
            }
        }
        if let (Some(J::Int(longest)), shortest) = (rule.get("maxLength"), bound(rule, "minLength"))
            && shortest.unwrap_or(0) > *longest
        {
            return Err(Refusal::new("schema_shape", at));
        }
        if rule.contains("enum") {
            let wrong = || Refusal::new("schema_shape", &self.step_str(at, "enum"));
            let Some(J::Array(members)) = rule.get("enum") else {
                return Err(wrong());
            };
            if members.is_empty()
                || members
                    .iter()
                    .any(|member| !matches!(member, J::Text(_) | J::Bool(_) | J::Int(_)))
            {
                return Err(wrong());
            }
            let distinct = members
                .iter()
                .enumerate()
                .all(|(at, member)| !members[..at].iter().any(|before| same(before, member)));
            if !distinct || members.iter().any(|member| !fits(member, rule, false)) {
                return Err(wrong());
            }
        }
        Ok(())
    }

    /// Values of the state, each a key of the answer schema that fits its rule
    /// (`render_spec._state`). What was submitted must have every required key.
    pub(crate) fn state(
        &self,
        value: &J,
        schema: &Obj,
        path: &str,
        submitted: bool,
    ) -> Result<(), Refusal> {
        let empty = Obj::new();
        let props = schema.get("properties").and_then(J::object).unwrap_or(&empty);
        let required = if submitted {
            required_of(&J::Object(schema.clone()))
        } else {
            Vec::new()
        };
        let required: Vec<&str> = required.iter().map(String::as_str).collect();
        let allows = |key: &Text| props.at(key).is_some();
        let fields = self.object(value, path, &allows, &required, "unknown_key")?;
        for key in json::ordered(fields)? {
            let rule = props.at(key).and_then(J::object).unwrap_or(&empty);
            if !fits(fields.at(key).unwrap_or(&J::Null), rule, true) {
                return Err(Refusal::new("state_value", &self.step(path, key)));
            }
        }
        Ok(())
    }

    /// A value of a property, by the catalog's schema for it (`render_spec._literal`).
    fn literal(&self, value: &J, rule: &J, path: &str) -> Result<(), Refusal> {
        let empty = Obj::new();
        let rule = rule.object().unwrap_or(&empty);
        let kind = rule.get("type").and_then(J::str);
        if kind == Some("object") || (rule.get("type").is_none() && rule.contains("properties")) {
            let declared = rule.get("properties").and_then(J::object).unwrap_or(&empty);
            let closed = rule.get("additionalProperties") == Some(&J::Bool(false));
            let allows = |key: &Text| !closed || declared.at(key).is_some();
            let required = required_of(&J::Object(rule.clone()));
            let required: Vec<&str> = required.iter().map(String::as_str).collect();
            let fields = self.object(value, path, &allows, &required, "unknown_key")?;
            for key in json::ordered(fields)? {
                if let Some(inner) = declared.at(key) {
                    let given = fields.at(key).unwrap_or(&J::Null);
                    self.literal(given, inner, &self.step(path, key))?;
                }
            }
            return Ok(());
        }
        let wrong = || Refusal::new("prop", path);
        if let Some(kind) = kind
            && TYPES.contains(&kind)
            && !of_type(value, kind)
        {
            return Err(wrong());
        }
        if rule.get("const").is_some_and(|constant| !same(value, constant)) {
            return Err(wrong());
        }
        if let Some(members) = rule.get("enum") {
            let listed = match members {
                J::Array(members) => members.iter().any(|member| same(value, member)),
                _ => false,
            };
            if !listed {
                return Err(wrong());
            }
        }
        match value {
            J::Text(text) => {
                let long = length(text.len());
                let within = bound(rule, "minLength").unwrap_or(0) <= long
                    && long <= bound(rule, "maxLength").unwrap_or(long);
                let plain = text
                    .as_str()
                    .is_some_and(|text| strip_hidden_blocks(text) == text);
                if !within || !plain {
                    return Err(wrong());
                }
            }
            J::Int(number) => {
                if !(bound(rule, "minimum").unwrap_or(*number) <= *number
                    && *number <= bound(rule, "maximum").unwrap_or(*number))
                {
                    return Err(wrong());
                }
            }
            J::Array(items) => {
                let many = length(items.len());
                if !(bound(rule, "minItems").unwrap_or(0) <= many
                    && many <= bound(rule, "maxItems").unwrap_or(many))
                {
                    return Err(wrong());
                }
                if let Some(inner) = rule.get("items") {
                    for (at, item) in items.iter().enumerate() {
                        self.literal(item, inner, &index(path, at))?;
                    }
                }
            }
            _ => {}
        }
        Ok(())
    }

    /// An expression of the state in the shape the catalog gives it: its one key is the
    /// operator, its value a pointer to one key of the answer schema
    /// (`render_spec._expression`). Answers the key.
    fn expression(
        &self,
        value: &J,
        shape: &J,
        path: &str,
        fields: &Obj,
    ) -> Result<String, Refusal> {
        let wrong = || Refusal::new("expression", path);
        self.literal(value, shape, path).map_err(|_| wrong())?;
        let operator = match shape.get("required") {
            Some(J::Array(required)) => required.first().and_then(J::str),
            _ => None,
        };
        let pointer = operator.and_then(|operator| value.get(operator)).and_then(J::str);
        let key = pointer
            .and_then(|pointer| pointer.strip_prefix('/'))
            .filter(|key| self.ident().matches(key) && fields.contains(key));
        key.map(str::to_string).ok_or_else(wrong)
    }

    /// The elements: one rooted tree of components of the catalog, each with properties as
    /// the catalog says, each key of the state bound by one control at most
    /// (`render_spec._elements`).
    fn elements(&self, spec: &Obj, schema: &Obj) -> Result<(), Refusal> {
        let path = "/spec/elements";
        let nodes = match spec.get("elements") {
            Some(J::Object(nodes)) if (ELEMENTS_FEWEST..=ELEMENTS_MOST).contains(&nodes.len()) => {
                nodes
            }
            _ => return Err(Refusal::new("element_count", path)),
        };
        let root = spec.get("root").unwrap_or(&J::Null);
        let root = root
            .str()
            .filter(|root| self.ident().matches(root) && nodes.contains(root))
            .ok_or_else(|| Refusal::new("element_id", "/spec/root"))?;
        let empty = Obj::new();
        let fields = schema.get("properties").and_then(J::object).unwrap_or(&empty);
        let mut bound = BTreeSet::new();
        for name in json::ordered(nodes)? {
            let at = self.step(path, name);
            if name
                .as_str()
                .is_none_or(|name| !self.ident().matches(name) || name == "on")
            {
                return Err(Refusal::new("element_id", &at));
            }
            let node = nodes.at(name).unwrap_or(&J::Null);
            self.element(node, &at, nodes, fields, &mut bound)?;
        }
        self.tree(nodes, root, path)
    }

    /// One element of the spec.
    fn element(
        &self,
        node: &J,
        at: &str,
        nodes: &Obj,
        fields: &Obj,
        bound: &mut BTreeSet<String>,
    ) -> Result<(), Refusal> {
        let node = self.closed(
            node,
            at,
            &["type", "props", "children"],
            &["type", "props"],
            "unknown_key",
        )?;
        let component = node
            .get("type")
            .and_then(J::str)
            .and_then(|kind| self.catalog.components.get(kind))
            .ok_or_else(|| Refusal::new("component", &format!("{at}/type")))?;
        let none = J::Array(Vec::new());
        let children = match node.get("children").unwrap_or(&none) {
            J::Array(children) if children.is_empty() || component.children => children,
            _ => return Err(Refusal::new("element_tree", &format!("{at}/children"))),
        };
        for (number, child) in children.iter().enumerate() {
            if !child
                .str()
                .is_some_and(|child| self.ident().matches(child) && nodes.contains(child))
            {
                let place = index(&format!("{at}/children"), number);
                return Err(Refusal::new("element_id", &place));
            }
        }
        let props = node.get("props").unwrap_or(&J::Null);
        self.props(props, component, &format!("{at}/props"), fields, bound)
    }

    /// The properties of an element.
    fn props(
        &self,
        props: &J,
        component: &Component,
        at: &str,
        fields: &Obj,
        bound: &mut BTreeSet<String>,
    ) -> Result<(), Refusal> {
        let empty = Obj::new();
        let rules = component.props.object().unwrap_or(&empty);
        let declared = rules.get("properties").and_then(J::object).unwrap_or(&empty);
        let closed = rules.get("additionalProperties") == Some(&J::Bool(false));
        let allows = |key: &Text| !closed || declared.at(key).is_some();
        let required = required_of(&component.props);
        let required: Vec<&str> = required.iter().map(String::as_str).collect();
        let props = self.object(props, at, &allows, &required, "unknown_key")?;
        let binding = |key: &str| {
            let found = component.bindings.iter().find(|(name, _)| name == key);
            found.map(|(_, what)| what.as_str())
        };
        let reading = |key: &str| {
            let found = component.reads.iter().find(|(name, _)| name == key);
            found.map(|(_, shape)| shape.as_str())
        };
        for key in json::ordered(props)? {
            let Some(rule) = declared.at(key) else {
                continue;
            };
            let (value, place) = (props.at(key).unwrap_or(&J::Null), self.step(at, key));
            let name = key.as_str().unwrap_or_default();
            if let Some(what) = binding(name) {
                let state_key = self.expression(value, rule, &place, fields)?;
                let field = fields.get(&state_key).unwrap_or(&J::Null);
                if bound.contains(&state_key) || !binds(what, field) {
                    return Err(Refusal::new("expression", &place));
                }
                bound.insert(state_key);
            } else if let (Some(shape), J::Object(_)) = (reading(name), value) {
                let shape = self.catalog.shapes.get(shape).unwrap_or(&J::Null);
                self.expression(value, shape, &place, fields)?;
            } else {
                self.literal(value, rule, &place)?;
            }
        }
        for (key, what) in &component.bindings {
            if what != OPTION_ORDER {
                continue;
            }
            let Some(value) = props.get(key) else {
                continue;
            };
            let operator = match declared.get(key).and_then(|rule| rule.get("required")) {
                Some(J::Array(required)) => required.first().and_then(J::str),
                _ => None,
            };
            let state_key = operator
                .and_then(|operator| value.get(operator))
                .and_then(J::str)
                .and_then(|pointer| pointer.strip_prefix('/'))
                .unwrap_or_default();
            let expected = fields.get(state_key).and_then(|rule| rule.get("enum"));
            let given: Option<Vec<J>> = match props.get("options") {
                Some(J::Array(options)) => Some(
                    options
                        .iter()
                        .map(|option| match option {
                            J::Object(fields) => fields.get("value").cloned().unwrap_or(J::Null),
                            _ => J::Null,
                        })
                        .collect(),
                ),
                _ => None,
            };
            if given.map(J::Array).as_ref() != expected {
                return Err(Refusal::new("expression", &format!("{at}/options")));
            }
        }
        Ok(())
    }

    /// The elements as one tree from the root: each once, none deeper than twelve, none
    /// where a component above it forbids it, and none left over.
    fn tree(&self, nodes: &Obj, root: &str, path: &str) -> Result<(), Refusal> {
        let mut seen: BTreeSet<String> = BTreeSet::new();
        let mut pending: Vec<(String, usize, BTreeSet<String>)> =
            vec![(root.to_string(), 1, BTreeSet::new())];
        while let Some((name, level, forbidden)) = pending.pop() {
            let at = self.step_str(path, &name);
            let node = nodes.get(&name).unwrap_or(&J::Null);
            let kind = node.get("type").and_then(J::str).unwrap_or_default();
            if seen.contains(&name) || forbidden.contains(kind) {
                return Err(Refusal::new("element_tree", &at));
            }
            if level > DEPTH_MOST {
                return Err(Refusal::new("depth", &at));
            }
            seen.insert(name);
            let mut below = forbidden;
            if let Some(component) = self.catalog.components.get(kind) {
                below.extend(component.forbid_descendants.iter().cloned());
            }
            if let Some(J::Array(children)) = node.get("children") {
                for child in children.iter().rev() {
                    let child = child.str().unwrap_or_default().to_string();
                    pending.push((child, level + 1, below.clone()));
                }
            }
        }
        if seen.len() != nodes.len() {
            return Err(Refusal::new("element_tree", path));
        }
        Ok(())
    }

    /// No key twice and no `on`, anywhere below (`render_spec.check_unique`).
    fn unique(&self, value: &J, path: &str, depth: usize) -> Result<(), Refusal> {
        if depth > UNIQUE_DEEPEST {
            return Err(Refusal::new("invalid_json", path));
        }
        match value {
            J::Object(fields) => {
                if fields.duplicate {
                    return Err(Refusal::new("invalid_object", path));
                }
                if fields.contains("on") {
                    return Err(Refusal::new("unknown_key", &self.step_str(path, "on")));
                }
                for key in json::ordered(fields)? {
                    let item = fields.at(key).unwrap_or(&J::Null);
                    self.unique(item, &self.step(path, key), depth + 1)?;
                }
            }
            J::Array(items) => {
                for (at, item) in items.iter().enumerate() {
                    self.unique(item, &index(path, at), depth + 1)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
}

/// Whether a key of the answer schema is what a control binds (`render_spec._binds`).
fn binds(what: &str, rule: &J) -> bool {
    if what == OPTION_ORDER {
        return matches!(rule.get("enum"), Some(J::Array(members)) if members.iter().all(|member| matches!(member, J::Text(_))));
    }
    rule.get("type").and_then(J::str) == Some(what)
}

/// Whether a payload is a render spec: the first rule it fails, in the reference's order
/// of stages (`render_spec.validate`).
pub fn validate(catalog: &Catalog, payload: &J) -> Result<(), Refusal> {
    let checks = Checks { catalog };
    let fields = checks.closed(payload, "", &FIELDS, &FIELDS, "unknown_key")?;
    if checks.canonical(payload, "")?.len() > PAYLOAD_MOST {
        return Err(Refusal::new("payload_size", ""));
    }
    let field = |name: &str| fields.get(name).unwrap_or(&J::Null);
    if *field("catalog") != catalog.name {
        return Err(Refusal::new("unknown_catalog", "/catalog"));
    }
    if !matches!(field("catalog_version"), J::Int(_)) || *field("catalog_version") != catalog.version
    {
        return Err(Refusal::new("catalog_version", "/catalog_version"));
    }
    match field("version") {
        J::Text(version) if (1..=VERSION_MOST).contains(&version.len()) => {}
        _ => return Err(Refusal::new("invalid_version", "/version")),
    }
    let spec = field("spec");
    if checks.canonical(spec, "")?.len() > SPEC_MOST {
        return Err(Refusal::new("spec_size", "/spec"));
    }
    checks.unique(spec, "/spec", 0)?;
    let parts = ["root", "elements", "state"];
    let spec = checks.closed(spec, "/spec", &parts, &parts, "unknown_key")?;
    let state = spec.get("state").unwrap_or(&J::Null);
    if checks.canonical(state, "")?.len() > STATE_MOST {
        return Err(Refusal::new("state_size", "/spec/state"));
    }
    let schema = field("answer_schema");
    checks.schema(schema)?;
    let empty = Obj::new();
    let schema = schema.object().unwrap_or(&empty);
    checks.elements(spec, schema)?;
    checks.state(state, schema, "/spec/state", false)?;
    let submit = checks.closed(
        field("submit"),
        "/submit",
        &["intent", "label"],
        &["intent", "label"],
        "unknown_key",
    )?;
    if !submit.get("intent").is_some_and(|intent| checks.ident().is(intent)) {
        return Err(Refusal::new("submit_intent", "/submit/intent"));
    }
    match submit.get("label") {
        Some(J::Text(label)) if (1..=LABEL_MOST).contains(&label.len()) => Ok(()),
        _ => Err(Refusal::new("submit_label", "/submit/label")),
    }
}
