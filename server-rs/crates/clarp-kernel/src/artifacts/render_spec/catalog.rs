//! The catalog `clarp.form` v1: the components a render spec may use, their properties, and
//! the words a refusal is told in.
//!
//! The two files are the contract's (`contract/catalogs/`) and both Hosts read the same
//! bytes: this one has them in its program. A catalog that does not pass the checks of
//! `render_spec._check_catalog` is not loaded, and then only render specs are refused
//! (`catalog_unavailable`); nothing else depends on it.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::json::{self, J, Obj, Text};
use super::{CODES, OPTION_ORDER};

/// The catalog file, as the contract has it.
pub const CATALOG: &[u8] = include_bytes!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../contract/catalogs/clarp.form.v1.json"
));
/// The sentence of every code, as the contract has them.
pub const REFUSALS: &[u8] = include_bytes!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../contract/catalogs/clarp.form.v1.refusals.json"
));

/// The catalog's own schema language: a catalog that uses another keyword is not loaded.
const SCHEMA_KEYWORDS: [&str; 13] = [
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
];
/// The types the schema language knows.
pub(crate) const TYPES: [&str; 5] = ["string", "boolean", "integer", "object", "array"];
/// What a binding may name: a type of the bound key, or the rule of the options' order.
const BINDINGS: [&str; 4] = ["string", "boolean", "integer", OPTION_ORDER];

/// The catalog could not be loaded: render specs are refused until it can be.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Unavailable;

/// The rule an identifier must pass: its first sign, the signs after it, and its length.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ident {
    first: BTreeSet<char>,
    rest: BTreeSet<char>,
    shortest: usize,
    longest: usize,
}

impl Ident {
    /// Whether a text is an identifier (`catalog().ident.fullmatch`).
    pub fn matches(&self, text: &str) -> bool {
        let mut signs = text.chars();
        let count = text.chars().count();
        (self.shortest..=self.longest).contains(&count)
            && signs
                .next()
                .is_some_and(|first| self.first.contains(&first))
            && signs.all(|sign| self.rest.contains(&sign))
    }

    /// Whether a value is a text that is an identifier (`render_spec._id`).
    pub fn is(&self, value: &J) -> bool {
        value.str().is_some_and(|text| self.matches(text))
    }
}

/// A component of the catalog.
#[derive(Debug, Clone, PartialEq)]
pub struct Component {
    /// The schema of its properties.
    pub props: J,
    /// Whether it may have children.
    pub children: bool,
    /// The components that may not stand below it.
    pub forbid_descendants: Vec<String>,
    /// Its properties that bind a key of the state, with what the key must be: in the
    /// order of their names.
    pub bindings: Vec<(String, String)>,
    /// Its properties that may read a key of the state, with the shape they read it in.
    pub reads: Vec<(String, String)>,
}

/// The catalog as it was loaded.
#[derive(Debug, Clone, PartialEq)]
pub struct Catalog {
    /// Its bytes, as they are served.
    pub raw: Vec<u8>,
    /// `c1:` and the SHA-256 of its bytes.
    pub sha256: String,
    pub ident: Ident,
    /// Its name and its version, as a payload must name them.
    pub name: J,
    pub version: J,
    pub components: BTreeMap<String, Component>,
    /// The shapes an expression may have, by its operator.
    pub shapes: BTreeMap<String, J>,
    /// The sentence of every code.
    reasons: BTreeMap<String, Value>,
}

/// The fields of an object, or nothing to load.
fn fields(value: Option<&J>) -> Result<&Obj, Unavailable> {
    value.and_then(J::object).ok_or(Unavailable)
}

fn text(value: &Text) -> Result<String, Unavailable> {
    value.as_str().map(str::to_string).ok_or(Unavailable)
}

/// The texts of a list.
fn texts(value: Option<&J>) -> Result<Vec<String>, Unavailable> {
    match value {
        None => Ok(Vec::new()),
        Some(J::Array(items)) => items
            .iter()
            .map(|item| item.str().map(str::to_string).ok_or(Unavailable))
            .collect(),
        Some(_) => Err(Unavailable),
    }
}

/// How many things a schema requires, for a schema whose `required` is a list.
fn required_count(rule: &J) -> Result<usize, Unavailable> {
    match rule.get("required") {
        None => Ok(0),
        Some(J::Array(items)) => Ok(items.len()),
        Some(_) => Err(Unavailable),
    }
}

/// Every schema in a schema of the catalog, for the check of its keywords
/// (`render_spec._schemas`).
fn schemas<'a>(rule: &'a J, found: &mut Vec<&'a Obj>) -> Result<(), Unavailable> {
    let fields = rule.object().ok_or(Unavailable)?;
    found.push(fields);
    match fields.get("properties") {
        Some(J::Object(children)) => {
            for (_, child) in children.iter() {
                schemas(child, found)?;
            }
        }
        Some(empty) if !empty.truthy() => {}
        None => {}
        Some(_) => return Err(Unavailable),
    }
    if let Some(items) = fields.get("items") {
        schemas(items, found)?;
    }
    Ok(())
}

/// A pair of names of a component (`bindings`, `reads`), each in what it may name.
fn named_pairs(
    component: &Obj,
    key: &str,
    declared: &Obj,
    allowed: &dyn Fn(&str) -> bool,
) -> Result<Vec<(String, String)>, Unavailable> {
    let pairs = match component.get(key) {
        None => return Ok(Vec::new()),
        Some(value) => fields(Some(value))?,
    };
    let mut named = Vec::new();
    for (name, what) in pairs.iter() {
        let what = what.str().ok_or(Unavailable)?;
        if declared.at(name).is_none() || !allowed(what) {
            return Err(Unavailable);
        }
        named.push((text(name)?, what.to_string()));
    }
    named.sort();
    Ok(named)
}

/// Two values equal as Python compares what its reader made: an object by its keys and their
/// values in any order, a number by its value whatever its type (a truth is a number too).
fn python_eq(one: &J, two: &J) -> bool {
    match (one, two) {
        (J::Object(one), J::Object(two)) => {
            one.len() == two.len()
                && one
                    .iter()
                    .all(|(key, value)| two.at(key).is_some_and(|other| python_eq(value, other)))
        }
        (J::Array(one), J::Array(two)) => {
            one.len() == two.len() && one.iter().zip(two).all(|(a, b)| python_eq(a, b))
        }
        (J::Text(one), J::Text(two)) => one == two,
        (J::Null, J::Null) => true,
        _ => match (numeric(one), numeric(two)) {
            (Some(Ok(a)), Some(Ok(b))) => a == b,
            (Some(a), Some(b)) => as_float(a) == as_float(b),
            _ => false,
        },
    }
}

/// A number as Python holds it: a whole one, or a fraction.
fn numeric(value: &J) -> Option<Result<i128, f64>> {
    match value {
        J::Int(number) => Some(Ok(*number)),
        J::Bool(truth) => Some(Ok(i128::from(*truth))),
        J::Float(number) => Some(Err(*number)),
        _ => None,
    }
}

#[allow(clippy::cast_precision_loss)]
fn as_float(number: Result<i128, f64>) -> f64 {
    match number {
        Ok(whole) => whole as f64,
        Err(fraction) => fraction,
    }
}

/// A whole number, as Python compares one: a truth value is one too.
fn whole(value: Option<&J>) -> Result<i128, Unavailable> {
    match value {
        Some(J::Int(number)) => Ok(*number),
        Some(J::Bool(truth)) => Ok(i128::from(*truth)),
        _ => Err(Unavailable),
    }
}

/// The signs a set of the identifier rule has: those of a text, or of every text of a list.
fn signs(value: Option<&J>) -> Result<BTreeSet<char>, Unavailable> {
    let all = match value {
        Some(J::Text(one)) => text(one)?,
        Some(J::Array(items)) => {
            let parts: Result<Vec<String>, Unavailable> = items
                .iter()
                .map(|item| item.text().ok_or(Unavailable).and_then(text))
                .collect();
            parts?.concat()
        }
        _ => return Err(Unavailable),
    };
    Ok(all.chars().collect())
}

impl Catalog {
    /// The catalog this Host has in its program.
    pub fn embedded() -> Result<Self, Unavailable> {
        Self::load(CATALOG, REFUSALS)
    }

    /// Loads a catalog from its bytes and the bytes of its refusals (`render_spec._loaded`
    /// and `_check_catalog`).
    pub fn load(raw: &[u8], refusals: &[u8]) -> Result<Self, Unavailable> {
        let data = json::read(raw).ok_or(Unavailable)?;
        let reasons = json::read(refusals).ok_or(Unavailable)?;
        let data = fields(Some(&data))?;
        let reasons = fields(Some(&reasons))?;
        let named = texts(Some(data.get("schema_keywords").ok_or(Unavailable)?))?;
        if named
            .iter()
            .any(|keyword| !SCHEMA_KEYWORDS.contains(&keyword.as_str()))
            || CODES.iter().any(|code| !reasons.contains(code))
        {
            return Err(Unavailable);
        }
        let shapes = fields(data.get("expression_shapes"))?;
        for (_, shape) in shapes.iter() {
            if required_count(shape)? != 1 {
                return Err(Unavailable);
            }
        }
        let mut found = Vec::new();
        for (_, shape) in shapes.iter() {
            schemas(shape, &mut found)?;
        }
        let all = fields(data.get("components"))?;
        let mut components = BTreeMap::new();
        for (name, component) in all.iter() {
            let component = fields(Some(component))?;
            let props = component.get("props").ok_or(Unavailable)?;
            schemas(props, &mut found)?;
            let empty = Obj::new();
            let declared = match props.get("properties") {
                Some(J::Object(declared)) => declared,
                _ => &empty,
            };
            let Some(J::Bool(children)) = component.get("children") else {
                return Err(Unavailable);
            };
            let forbid_descendants = texts(component.get("forbid_descendants"))?;
            if forbid_descendants
                .iter()
                .any(|name| !all.contains(name.as_str()))
            {
                return Err(Unavailable);
            }
            let bindings = named_pairs(component, "bindings", declared, &|what| {
                BINDINGS.contains(&what)
            })?;
            let reads = named_pairs(component, "reads", declared, &|what| shapes.contains(what))?;
            // The file says the shape of a binding twice: the validator reads the property's
            // copy, so it must be the shape itself (the reference's PR 430).
            let bind_shape = shapes.get("$bindState");
            for (key, _) in &bindings {
                let bound = declared.get(key).ok_or(Unavailable)?;
                if !bind_shape.is_some_and(|shape| python_eq(bound, shape)) {
                    return Err(Unavailable);
                }
            }
            components.insert(
                text(name)?,
                Component {
                    props: props.clone(),
                    children: *children,
                    forbid_descendants,
                    bindings,
                    reads,
                },
            );
        }
        for rule in found {
            let kind = match rule.get("type") {
                None => "string",
                Some(kind) => kind.str().ok_or(Unavailable)?,
            };
            let known = rule.keys().all(|key| {
                key.as_str()
                    .is_some_and(|key| named.iter().any(|n| n == key))
            });
            if !known || !TYPES.contains(&kind) {
                return Err(Unavailable);
            }
        }
        let rule = fields(data.get("identifiers"))?;
        let (shortest, longest) = (whole(rule.get("minLength"))?, whole(rule.get("maxLength"))?);
        let first = signs(rule.get("first"))?;
        let rest = signs(rule.get("rest"))?;
        if !(1 <= shortest && shortest <= longest) || first.is_empty() || rest.is_empty() {
            return Err(Unavailable);
        }
        let ident = Ident {
            first,
            rest,
            shortest: usize::try_from(shortest).map_err(|_| Unavailable)?,
            longest: usize::try_from(longest).map_err(|_| Unavailable)?,
        };
        let mut shaped = BTreeMap::new();
        for (operator, shape) in shapes.iter() {
            shaped.insert(text(operator)?, shape.clone());
        }
        let digest: String = Sha256::digest(raw)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Ok(Self {
            raw: raw.to_vec(),
            sha256: format!("c1:{digest}"),
            ident,
            name: data.get("catalog").ok_or(Unavailable)?.clone(),
            version: data.get("catalog_version").ok_or(Unavailable)?.clone(),
            components,
            shapes: shaped,
            reasons: reasons
                .iter()
                .map(|(code, sentence)| (code.lossy(), sentence.to_value()))
                .collect(),
        })
    }

    /// The sentence of a code.
    pub fn reason(&self, code: &str) -> Value {
        self.reasons.get(code).cloned().unwrap_or(Value::Null)
    }
}
