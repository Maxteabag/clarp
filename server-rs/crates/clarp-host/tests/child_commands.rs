//! Every production command starts with the Host's child environment.
#![allow(clippy::unwrap_used)]
use proc_macro2::{TokenStream, TokenTree};
use std::collections::BTreeSet;
use std::path::Path;
use syn::visit::{self, Visit};

#[derive(Default)]
struct Commands {
    found: bool,
    aliases: BTreeSet<String>,
}

fn test_item(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|attr| {
        attr.path().is_ident("test")
            || attr.path().is_ident("cfg")
                && attr
                    .parse_args::<syn::Ident>()
                    .is_ok_and(|arg| arg == "test")
    })
}

/// Imports and type aliases can give the process command another local name.
#[derive(Default)]
struct Aliases(BTreeSet<String>);
impl<'ast> Visit<'ast> for Aliases {
    fn visit_use_rename(&mut self, rename: &'ast syn::UseRename) {
        if rename.ident == "Command" {
            self.0.insert(rename.rename.to_string());
        }
    }
    fn visit_item_type(&mut self, item: &'ast syn::ItemType) {
        if let syn::Type::Path(ty) = item.ty.as_ref()
            && ty
                .path
                .segments
                .last()
                .is_some_and(|s| s.ident == "Command")
        {
            self.0.insert(item.ident.to_string());
        }
    }
}

impl<'ast> Visit<'ast> for Commands {
    fn visit_item_mod(&mut self, item: &'ast syn::ItemMod) {
        if !test_item(&item.attrs) {
            visit::visit_item_mod(self, item);
        }
    }
    fn visit_item_fn(&mut self, item: &'ast syn::ItemFn) {
        if !test_item(&item.attrs) {
            visit::visit_item_fn(self, item);
        }
    }
    fn visit_item_impl(&mut self, item: &'ast syn::ItemImpl) {
        if !test_item(&item.attrs) {
            visit::visit_item_impl(self, item);
        }
    }
    fn visit_path(&mut self, path: &'ast syn::Path) {
        let mut segments = path.segments.iter().rev();
        if segments.next().is_some_and(|s| s.ident == "new")
            && segments.next().is_some_and(|s| {
                s.ident == "Command" || self.aliases.contains(&s.ident.to_string())
            })
        {
            self.found = true;
        }
        visit::visit_path(self, path);
    }
    fn visit_macro(&mut self, mac: &'ast syn::Macro) {
        // Macro arguments are token trees rather than parsed Rust expressions.
        self.found |= macro_command(mac.tokens.clone(), &self.aliases);
    }
}

fn macro_command(tokens: TokenStream, aliases: &BTreeSet<String>) -> bool {
    let mut phase = 0;
    for token in tokens {
        match token {
            TokenTree::Ident(id) if phase == 3 && id == "new" => return true,
            TokenTree::Ident(id) if id == "Command" || aliases.contains(&id.to_string()) => {
                phase = 1
            }
            TokenTree::Punct(p) if (phase == 1 || phase == 2) && p.as_char() == ':' => phase += 1,
            TokenTree::Group(g) if macro_command(g.stream(), aliases) => return true,
            _ => phase = 0,
        }
    }
    false
}

fn constructs_command(source: &str) -> bool {
    let file = syn::parse_file(source).unwrap();
    let mut aliases = Aliases::default();
    aliases.visit_file(&file);
    let mut commands = Commands {
        aliases: aliases.0,
        found: false,
    };
    commands.visit_file(&file);
    commands.found
}

// Paths are relative to the workspace. Exemptions apply to one file, never
// to an entire dependency (in particular the shim's linked protocol library).
const EXEMPT: &[(&str, &str)] = &[
    (
        "crates/clarp-child/src/lib.rs",
        "Owns the standard command constructor and common environment.",
    ),
    (
        "crates/clarp-sealed/src/lib.rs",
        "Builds the commands tests start, from nothing; only tests depend on it.",
    ),
    (
        "crates/clarp-host/src/child.rs",
        "Builds the PTY adapter with the common environment.",
    ),
    (
        "crates/clarp-shim/src/main.rs",
        "Standalone shim executable starts the assigned tool after the Host supplied its environment.",
    ),
    (
        "crates/clarp-shim/src/opencode.rs",
        "Only the shim binary's main declares this module and runs the OpenCode driver; it is absent from the linked protocol library.",
    ),
];

fn inspect(
    dir: &Path,
    exempt: &BTreeSet<std::path::PathBuf>,
    offenders: &mut Vec<std::path::PathBuf>,
) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            inspect(&path, exempt, offenders);
        } else if path.extension().is_some_and(|ext| ext == "rs")
            && !exempt.contains(&path)
            && constructs_command(&std::fs::read_to_string(&path).unwrap())
        {
            offenders.push(path);
        }
    }
}

fn exemptions(workspace: &Path, entries: &[(&str, &str)]) -> BTreeSet<std::path::PathBuf> {
    entries
        .iter()
        .map(|(path, reason)| {
            assert!(!reason.trim().is_empty(), "missing reason for {path}");
            let file = workspace.join(path);
            assert!(file.is_file(), "missing exempt file {path}");
            file
        })
        .collect()
}

#[test]
fn production_commands_start_at_the_child_constructor() {
    let host = Path::new(env!("CARGO_MANIFEST_DIR"));
    let metadata = std::process::Command::new(env!("CARGO"))
        .args([
            "metadata",
            "--no-deps",
            "--locked",
            "--offline",
            "--format-version",
            "1",
        ])
        .current_dir(host)
        .output()
        .unwrap();
    assert!(
        metadata.status.success(),
        "{}",
        String::from_utf8_lossy(&metadata.stderr)
    );
    let manifest: serde_json::Value = serde_json::from_slice(&metadata.stdout).unwrap();
    let workspace = Path::new(manifest["workspace_root"].as_str().unwrap());
    let exempt = exemptions(workspace, EXEMPT);
    let packages = manifest["packages"].as_array().unwrap();
    let mut read = BTreeSet::new();
    let mut offenders = Vec::new();
    for package in packages {
        let dir = Path::new(package["manifest_path"].as_str().unwrap())
            .parent()
            .unwrap();
        let src = dir.join("src");
        inspect(&src, &exempt, &mut offenders);
        for target in package["targets"].as_array().unwrap() {
            if target["kind"]
                .as_array()
                .unwrap()
                .iter()
                .any(|k| k == "lib" || k == "bin")
            {
                assert!(
                    Path::new(target["src_path"].as_str().unwrap()).starts_with(&src),
                    "unexamined target {target}"
                );
            }
        }
        read.insert(dir.to_path_buf());
    }
    for member in packages {
        let dir = Path::new(member["manifest_path"].as_str().unwrap())
            .parent()
            .unwrap();
        assert!(
            read.contains(dir),
            "workspace crate was never inspected: {}",
            dir.display()
        );
    }
    // Cargo --no-deps also lists local dependency paths. A crate outside
    // the set examined above must not silently fall out of coverage.
    for package in packages {
        for dependency in package["dependencies"].as_array().unwrap() {
            if let Some(dir) = dependency["path"].as_str() {
                assert!(
                    read.contains(Path::new(dir)),
                    "unexamined local dependency {dir}"
                );
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "Use the shared command constructors: {offenders:?}"
    );
}

#[test]
#[should_panic(expected = "missing reason")]
fn a_named_exemption_must_explain_the_process_boundary() {
    exemptions(
        Path::new(env!("CARGO_MANIFEST_DIR")),
        &[("src/lib.rs", " ")],
    );
}

#[test]
fn the_guard_sees_constructors_in_production_items_and_macro_arguments() {
    for expression in [
        "Command::new(program)",
        "std::process::Command :: new(program)",
        "tokio::process::Command::new(program)",
        "outer!(inner!(pty_process::Command::new(program)))",
    ] {
        let example = format!("#[cfg(test)] mod tests {{}} fn start() {{ {expression}; }}");
        assert!(constructs_command(&example), "{expression}");
    }
    assert!(constructs_command(
        "use std::process::Command as Spawn; fn run(){Spawn::new(x);}"
    ));
    assert!(constructs_command(
        "type Spawn=tokio::process::Command; fn run(){wrap!(Spawn::new(x));}"
    ));
    assert!(!constructs_command(
        r#"
        #[cfg(test)] mod tests {fn child(){Command::new("fixture");}}
        #[test] fn isolated(){Command::new("fixture");}
        fn ordinary(){ println!("Command::new(program)"); }
    "#
    ));
}

#[test]
fn the_guard_reports_production_files_even_beside_or_after_test_modules() {
    let dir = std::env::temp_dir().join(format!("clarp-constructor-check-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("nested")).unwrap();
    let door = dir.join("child.rs");
    let other = dir.join("nested/child.rs");
    let text = "#[cfg(test)] mod tests {} fn run(){ Command::new(program); }";
    std::fs::write(&door, text).unwrap();
    std::fs::write(&other, text).unwrap();
    let mut offenders = Vec::new();
    inspect(&dir, &BTreeSet::from([door]), &mut offenders);
    std::fs::remove_dir_all(&dir).unwrap();
    assert_eq!(offenders, [other]);
}
