//! The translations of Rust text (`clarp_core::tr!`): the same .po files Slint
//! bundles (desktop-rs/i18n/<lang>/LC_MESSAGES/clarp-slint.po), read with the
//! parser Slint's compiler uses and written as a sorted table to OUT_DIR.

use std::fmt::Write;
use std::path::Path;

/// `clarp_core::i18n::LANGUAGES` without English, in its order.
const LANGUAGES: [&str; 5] = ["nb", "zh-Hans", "hi", "es", "fr"];

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../i18n");
    println!("cargo:rerun-if-changed={}", root.display());
    let mut out = String::from("pub static CATALOG: [&[(&str, &str, &[&str])]; 6] = [\n    &[],\n");
    let mut headers = String::from("pub static PLURAL_HEADERS: &[(&str, &str)] = &[\n");
    for language in LANGUAGES {
        let path = root.join(language).join("LC_MESSAGES").join("clarp-slint.po");
        println!("cargo:rerun-if-changed={}", path.display());
        let mut entries: Vec<(String, String, Vec<String>)> = Vec::new();
        if path.exists() {
            let file = rspolib::pofile(path.as_path()).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
            if let Some(forms) = file.metadata.get("Plural-Forms") {
                writeln!(headers, "    ({language:?}, {:?}),", forms.trim()).unwrap();
            }
            for entry in &file.entries {
                // What Slint's compiler takes: translated, not fuzzy or obsolete.
                if !rspolib::prelude::TranslatedEntry::translated(entry) {
                    continue;
                }
                let forms = if entry.msgstr_plural.is_empty() { entry.msgstr.clone().into_iter().collect() } else { entry.msgstr_plural.clone() };
                entries.push((entry.msgctxt.clone().unwrap_or_default(), entry.msgid.clone(), forms));
            }
        }
        entries.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
        entries.dedup_by(|a, b| a.0 == b.0 && a.1 == b.1);
        out.push_str("    &[\n");
        for (context, text, forms) in entries {
            writeln!(out, "        ({context:?}, {text:?}, &{forms:?}),").unwrap();
        }
        out.push_str("    ],\n");
    }
    out.push_str("];\n");
    headers.push_str("];\n");
    let target = Path::new(&std::env::var("OUT_DIR").unwrap()).join("i18n_catalog.rs");
    std::fs::write(target, out + &headers).unwrap();
}
