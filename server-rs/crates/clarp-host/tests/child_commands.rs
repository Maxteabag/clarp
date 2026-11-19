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

fn inspect(dir: &Path, door: &Path, offenders: &mut Vec<std::path::PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            inspect(&path, door, offenders);
        } else if path.extension().is_some_and(|ext| ext == "rs")
            && path != door
            && constructs_command(&std::fs::read_to_string(&path).unwrap())
        {
            offenders.push(path);
        }
    }
}

#[test]
fn production_commands_start_at_the_child_constructor() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut offenders = Vec::new();
    inspect(&src, &src.join("child.rs"), &mut offenders);
    assert!(
        offenders.is_empty(),
        "Use the constructors in child.rs: {offenders:?}"
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
    inspect(&dir, &door, &mut offenders);
    std::fs::remove_dir_all(&dir).unwrap();
    assert_eq!(offenders, [other]);
}
