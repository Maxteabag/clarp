//! Who a client says it is: `X-Clarp-Client: <platform>/<build> contract=<n>`, read as the
//! Python Host reads it (`server_identity.render_spec_capable`). One reader for every rule that
//! asks it (a render spec's threshold, the model of an agent's live call).

/// The client's name and contract, where the header says them exactly so: a name of letters,
/// digits, `_` and `-` (a letter first, 32 signs at most), a build of 1 to 80 letters, digits
/// and `._-`, and `contract=` with 1 to 9 digits. Anything else says nothing.
pub fn named(header: &str) -> Option<(&str, i64)> {
    let (platform, rest) = header.split_once('/')?;
    let mut signs = platform.chars();
    let platform_ok = signs
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic())
        && platform.len() <= 32
        && signs.all(|sign| sign.is_ascii_alphanumeric() || sign == '_' || sign == '-');
    let (build, contract) = rest.split_once(' ')?;
    let build_ok = (1..=80).contains(&build.len())
        && build
            .chars()
            .all(|sign| sign.is_ascii_alphanumeric() || matches!(sign, '.' | '_' | '-'));
    let number = contract.strip_prefix("contract=")?;
    let number_ok = (1..=9).contains(&number.len()) && number.bytes().all(|b| b.is_ascii_digit());
    if !(platform_ok && build_ok && number_ok) {
        return None;
    }
    Some((platform, number.parse::<i64>().ok()?))
}

/// What the owner's table of minimums by client name comes to (the Python Host's one rule for
/// both of its tables, a render spec's and the live model's): a key is a client's name in the
/// header's own form ([`named`] reads it) and counts exactly as written; its value is a whole
/// number of at least 0 (0: never for that name).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Minimums {
    /// What is taken, by the name exactly as written.
    pub taken: std::collections::BTreeMap<String, i64>,
    /// The keys refused, as a line of the journal may name them: not a client's name, or a
    /// value that is no whole number of at least 0.
    pub refused: Vec<String>,
    /// The keys taken that differ from a name the Host knows by itself only in their case: the
    /// key, and that name. Each counts for a client that sends it so, and for no other.
    pub cased: Vec<(String, String)>,
}

impl Minimums {
    /// What the reading of a table says, in the words of the one rule for every such table: the
    /// keys refused, where there are any, then each key taken that differs from a known name
    /// only in its case. Whose table it is, the caller says.
    pub fn lines(&self) -> Vec<String> {
        let mut said = Vec::new();
        if !self.refused.is_empty() {
            said.push(format!(
                "refused entries (a client's name as its header sends it, and a whole number of at least 0): {}",
                self.refused.join(", ")
            ));
        }
        for (key, name) in &self.cased {
            let (key, name) = (as_said(key), as_said(name));
            said.push(format!(
                "{key} is taken as written: it counts for a client that sends {key}, not for {name}"
            ));
        }
        said
    }
}

/// Reads the owner's table: each key with its value where that is a whole number (`None` for
/// anything else: a text, a fraction, true or false), and the names the Host knows by itself.
pub fn minimums<'a>(
    entries: impl IntoIterator<Item = (&'a str, Option<i64>)>,
    built_in: &[&str],
) -> Minimums {
    let mut read = Minimums::default();
    for (key, value) in entries {
        // A key is a name a client's header could carry, and nothing more.
        let a_name = named(&format!("{key}/1 contract=1")).is_some_and(|(name, _)| name == key);
        match value.filter(|minimum| *minimum >= 0) {
            Some(minimum) if a_name => {
                let differs = |name: &&&str| **name != key && name.eq_ignore_ascii_case(key);
                if let Some(name) = built_in.iter().find(differs) {
                    read.cased.push((key.to_string(), (*name).to_string()));
                }
                read.taken.insert(key.to_string(), minimum);
            }
            _ => read.refused.push(as_said(key)),
        }
    }
    read
}

/// A key of the owner's as a line of the journal names it: quoted, at most 40 signs, and a
/// sign a line does not hold as `?`.
pub fn as_said(key: &str) -> String {
    let mut said: String = key
        .chars()
        .take(40)
        .map(|sign| {
            if sign.is_ascii_graphic() || sign == ' ' {
                sign
            } else {
                '?'
            }
        })
        .collect();
    if key.chars().count() > 40 {
        said.push_str("...");
    }
    format!("'{said}'")
}
