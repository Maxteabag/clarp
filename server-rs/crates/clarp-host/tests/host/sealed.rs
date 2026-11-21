//! What a process a Host test starts is given of the environment: the rule of `clarp-sealed`,
//! with every program a person installs that the Host can start taken from the product's own
//! tables, so that a tool added to them later is kept from the machine without a word here.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::Path;

pub(crate) use clarp_sealed::{ABSENT, NOT_READ, PATH, absent, home, runner};

/// Every program a person installs that the Host can start: the variable that names it
/// instead, and the name it is otherwise found by on the path.
pub(crate) fn programs() -> Vec<(&'static str, &'static str)> {
    let mut all: Vec<(&str, &str)> = clarp_backend::PROGRAMS
        .iter()
        .map(|&(_, variable, name)| (variable, name))
        .collect();
    let (_, variable, name) = clarp_host::launch_directories::PROGRAM;
    all.push((variable, name));
    all.sort_unstable();
    all.dedup();
    all
}

/// The environment a process a Host test starts in `place` is given, on a machine whose
/// environment is `runner`, with what of it the test `named` on purpose.
pub(crate) fn sealed(
    runner: &[(OsString, OsString)],
    place: &Path,
    named: &[&str],
) -> BTreeMap<OsString, OsString> {
    clarp_sealed::sealed(runner, place, &programs(), named)
}
