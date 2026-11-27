//! The skills kept installed for the tools: kept by a Host started with `--accounts select`
//! and a tree of skills, and by no other. Every folder is a stand-in under the test's place.
use super::*;
use std::path::Path;

/// A stand-in machine: a home with the tools' skill folders, this Host's release of skills
/// (and an older one), the releases of the Python Host that kept the tools before, its record
/// of the chosen skills, and a directory of programs for the requirements.
struct Machine {
    home: PathBuf,
    tree: PathBuf,
    older: PathBuf,
    python: PathBuf,
    seed: PathBuf,
    programs: PathBuf,
}

const SEED: &str =
    "{\"channel\": \"stable\", \"skills\": [\"gone\"], \"skills_off\": [\"core1\", \"opt2\"]}";

fn skill(tree: &Path, id: &str) {
    std::fs::create_dir_all(tree.join(id)).unwrap();
    std::fs::write(
        tree.join(id).join("SKILL.md"),
        format!("---\nname: {id}\n---\n"),
    )
    .unwrap();
}

fn link(place: &Path, target: &Path) {
    std::fs::create_dir_all(place.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(target, place).unwrap();
}

impl Machine {
    fn new(place: &Place) -> Self {
        let root = &place.root;
        let home = root.join("home");
        let tree = root.join("share/releases/r2/skills");
        let older = root.join("share/releases/r1/skills");
        let python = root.join("python-share/releases");
        let programs = root.join("programs");
        let manifest = json!({ "version": 1, "skills": [
            { "id": "core1", "pack": "core", "description": "First core" },
            { "id": "core2", "pack": "core", "description": "Core of a newer release" },
            { "id": "art1", "pack": "artifacts", "description": "Artifacts", "default_enabled": false,
              "requires": [root.join("programs/clarp-standin-tool").to_str().unwrap()] },
            { "id": "opt1", "pack": "optional", "description": "Either program",
              "requires": ["clarp-absent-tool or clarp-standin-tool"] },
            { "id": "opt2", "pack": "optional", "description": "A folder, no program",
              "requires": ["clarp-dir-tool"] },
            { "id": "dep", "pack": "optional", "description": "Chosen, program not runnable",
              "requires": ["clarp-plain-tool"] },
            { "id": "mine", "pack": "optional", "description": "The user has one" },
            { "id": "agyown", "pack": "optional", "description": "Antigravity has one" },
            { "id": "nosrc", "pack": "optional", "description": "Not shipped" },
        ]});
        std::fs::create_dir_all(&tree).unwrap();
        std::fs::write(tree.join("manifest.json"), manifest.to_string()).unwrap();
        for id in [
            "core1", "core2", "art1", "opt1", "opt2", "dep", "mine", "agyown",
        ] {
            skill(&tree, id);
            skill(&older, id);
            skill(&python.join("r0/skills"), id);
        }
        skill(&python.join("r0/skills"), "gone");
        let (claude, codex) = (home.join(".claude/skills"), home.join(".codex/skills"));
        let agy = home.join(".gemini/config/skills");
        // What the Python Host and an older release of this one left, and what is the user's.
        link(&claude.join("core1"), &python.join("r0/skills/core1"));
        link(&codex.join("core1"), &older.join("core1"));
        link(&claude.join("gone"), &python.join("r0/skills/gone"));
        std::fs::create_dir_all(root.join("elsewhere/core2")).unwrap();
        link(&codex.join("core2"), &root.join("elsewhere/core2"));
        std::fs::create_dir_all(claude.join("mine")).unwrap();
        std::fs::write(claude.join("mine/SKILL.md"), "the user's own\n").unwrap();
        std::fs::create_dir_all(agy.join("agyown")).unwrap();
        std::fs::write(agy.join("agyown/SKILL.md"), "Antigravity's own\n").unwrap();
        std::fs::create_dir_all(&programs).unwrap();
        let program = programs.join("clarp-standin-tool");
        std::fs::write(&program, "#!/bin/sh\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
        // Found on the path, and no program: a file that does not run, and a folder.
        std::fs::write(programs.join("clarp-plain-tool"), "not run\n").unwrap();
        std::fs::create_dir_all(programs.join("clarp-dir-tool")).unwrap();
        let seed = root.join("python-config/install.json");
        std::fs::create_dir_all(seed.parent().unwrap()).unwrap();
        std::fs::write(&seed, SEED).unwrap();
        Self {
            home,
            tree,
            older,
            python,
            seed,
            programs,
        }
    }

    fn folder(&self, tool: &str) -> PathBuf {
        match tool {
            "claude" => self.home.join(".claude/skills"),
            "codex" => self.home.join(".codex/skills"),
            _ => self.home.join(".gemini/config/skills"),
        }
    }

    /// Where a tool's link for a skill leads, `None` where there is no link.
    fn leads(&self, tool: &str, id: &str) -> Option<PathBuf> {
        std::fs::read_link(self.folder(tool).join(id)).ok()
    }

    fn args(&self, tree: &Path, accounts: Option<&str>) -> Vec<String> {
        let mut args = vec![
            "--skills".to_owned(),
            tree.to_str().unwrap().to_owned(),
            "--skills-other-releases".to_owned(),
            self.python.to_str().unwrap().to_owned(),
            "--skills-chosen-before".to_owned(),
            self.seed.to_str().unwrap().to_owned(),
        ];
        if let Some(mode) = accounts {
            args.extend(["--accounts".to_owned(), mode.to_owned()]);
        }
        args
    }

    /// A Host of this release; one that keeps the tools is waited for until it has reconciled
    /// their links, which it does beside its start.
    fn start(&self, place: &Place, accounts: Option<&str>) -> Host {
        let host = self.start_of(place, &self.tree, accounts, None);
        if accounts == Some("select") {
            wait_for("the skills reconciled", || reconciled(&host) > 0);
        }
        host
    }

    /// A Host of the release whose skills are `tree`, its reconciliation held while `hold`
    /// exists, not waited for.
    fn start_of(
        &self,
        place: &Place,
        tree: &Path,
        accounts: Option<&str>,
        hold: Option<&Path>,
    ) -> Host {
        let path = format!("{}:{}", self.programs.display(), sealed::PATH);
        let args = self.args(tree, accounts);
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        let mut world = vec![
            ("HOME", self.home.to_str().unwrap()),
            ("PATH", path.as_str()),
        ];
        if let Some(hold) = hold {
            world.push(("CLARP_TEST_SKILLS_HELD_WHILE", hold.to_str().unwrap()));
        }
        let without = [
            "CODEX_HOME",
            "CLARP_CLAUDE_SKILLS",
            "CLARP_CODEX_SKILLS",
            "CLARP_AGY_SKILLS",
        ];
        place.start_without("fixture", &args, &world, &without)
    }

    /// Every entry of the tools' skill folders, the release trees and the other Host's record,
    /// as it is on the disk, byte for byte.
    fn snapshot(&self) -> Vec<(String, String)> {
        fn walk(at: &Path, base: &Path, into: &mut Vec<(String, String)>) {
            let mut entries: Vec<PathBuf> = std::fs::read_dir(at)
                .map(|read| read.map(|entry| entry.unwrap().path()).collect())
                .unwrap_or_default();
            entries.sort();
            for entry in entries {
                let name = entry.strip_prefix(base).unwrap().display().to_string();
                let found = std::fs::symlink_metadata(&entry).unwrap();
                if found.file_type().is_symlink() {
                    let target = std::fs::read_link(&entry).unwrap();
                    into.push((name, format!("link {}", target.display())));
                } else if found.is_dir() {
                    into.push((name, "dir".to_owned()));
                    walk(&entry, base, into);
                } else {
                    let bytes = std::fs::read(&entry).unwrap();
                    into.push((name, format!("file {}", String::from_utf8_lossy(&bytes))));
                }
            }
        }
        let mut all = Vec::new();
        let tools = [
            self.home.join(".claude/skills"),
            self.home.join(".codex/skills"),
            self.home.join(".gemini/config"),
        ];
        let trees = [
            self.seed.parent().unwrap(),
            self.tree.parent().unwrap(),
            self.python.as_path(),
        ];
        for at in tools.iter().map(PathBuf::as_path).chain(trees) {
            all.push((at.display().to_string(), String::new()));
            walk(at, at.parent().unwrap(), &mut all);
        }
        all
    }
}

/// How many reconciliations a Host has finished whole, as its journal says.
fn reconciled(host: &Host) -> usize {
    host.heard
        .lock()
        .unwrap()
        .matches("clarp-host: skills reconciled in ")
        .count()
}

fn row(listed: &Value, id: &str) -> Value {
    listed["skills"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["id"] == id)
        .unwrap_or_else(|| panic!("no row {id} in {listed}"))
        .clone()
}

#[test]
fn both_managed_skills_routes_refuse_a_malformed_manifest_before_using_it() {
    let place = Place::new("skills-malformed-manifest");
    let machine = Machine::new(&place);
    let host = machine.start(&place, Some("select"));
    let manifest = json!({"skills": [{"id": "opt2"}, {"id": "other", "requires": false}]});
    std::fs::write(machine.tree.join("manifest.json"), manifest.to_string()).unwrap();

    let refused = (503, json!({"error": "Clarp skill manifest is unavailable"}));
    assert_eq!(host.ask("GET", "/managed-skills", None), refused);
    assert_eq!(
        host.ask(
            "POST",
            "/managed-skills",
            Some(&json!({"skill_id": "opt2", "enabled": true}))
        ),
        refused
    );
    assert_eq!(machine.leads("claude", "opt2"), None);
    wait_for("both bad-manifest log lines", || {
        host.heard
            .lock()
            .unwrap()
            .matches("managedSkillsManifestBad item=1 requires is not a list")
            .count()
            == 2
    });
}

#[test]
fn a_host_that_keeps_the_tools_keeps_their_skills_installed() {
    let place = Place::new("skills-kept");
    let machine = Machine::new(&place);
    let host = machine.start(&place, Some("select"));
    let tree = std::fs::canonicalize(&machine.tree).unwrap();
    let ours = |id: &str| Some(tree.join(id));

    let state_path = place.root.join("data/install.json");
    let state =
        || -> Value { serde_json::from_slice(&std::fs::read(&state_path).unwrap()).unwrap() };
    // Start with explicit opt-outs, even when the previous on-list listed only a retired skill.
    assert_eq!(state()["skills_off"], json!(["core1", "opt2"]));
    assert_eq!(
        state()["skills"],
        json!([
            "agyown", "art1", "core1", "core2", "dep", "mine", "nosrc", "opt1"
        ])
    );
    for tool in ["claude", "codex", "agy"] {
        assert_eq!(machine.leads(tool, "opt1"), ours("opt1"), "{tool}");
        assert_eq!(machine.leads(tool, "dep"), ours("dep"), "{tool}");
        assert_eq!(machine.leads(tool, "core1"), ours("core1"), "{tool}");
        assert_eq!(
            machine.leads(tool, "art1"),
            ours("art1"),
            "{tool}: on by default"
        );
        assert_eq!(machine.leads(tool, "opt2"), None, "{tool}: opted out");
    }
    assert_eq!(machine.leads("claude", "core2"), ours("core2"));
    // What is not Clarp's stays: a link to elsewhere, the user's own skill, Antigravity's own.
    assert_eq!(
        machine.leads("codex", "core2"),
        Some(place.root.join("elsewhere/core2"))
    );
    assert_eq!(
        std::fs::read_to_string(machine.folder("claude").join("mine/SKILL.md")).unwrap(),
        "the user's own\n"
    );
    assert!(!machine.folder("agy").join("agyown").is_symlink());
    // A chosen skill this release does not have loses the link another release left.
    assert!(!machine.folder("claude").join("gone").exists());
    assert!(!machine.folder("claude").join("gone").is_symlink());
    // The other Host's record is read, never written.
    assert_eq!(std::fs::read_to_string(&machine.seed).unwrap(), SEED);

    // The status, row by row, in the manifest's order, with the reference's fields.
    let listed = host.get("/managed-skills");
    let ids: Vec<&str> = listed["skills"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        ids,
        [
            "core1", "core2", "art1", "opt1", "opt2", "dep", "mine", "agyown", "nosrc"
        ]
    );
    assert_eq!(
        row(&listed, "core1"),
        json!({ "id": "core1", "pack": "core", "description": "First core", "enabled": true,
                "optional": false, "health": "healthy",
                "links": { "claude": "healthy", "codex": "healthy", "agy": "healthy" },
                "requirements_ok": true, "missing_requirements": [] })
    );
    let health = |id: &str| row(&listed, id)["health"].as_str().unwrap().to_owned();
    assert_eq!(health("core2"), "modified");
    assert_eq!(row(&listed, "core2")["links"]["codex"], "modified");
    assert_eq!(health("art1"), "healthy");
    assert!(row(&listed, "art1").get("default_enabled").is_none());
    assert_eq!(
        row(&listed, "art1")["requirements_ok"],
        true,
        "a program named by its path"
    );
    assert_eq!(row(&listed, "art1")["optional"], true);
    assert_eq!(health("opt1"), "healthy");
    assert_eq!(health("opt2"), "inactive");
    assert_eq!(
        row(&listed, "opt2")["missing_requirements"],
        json!(["clarp-dir-tool"])
    );
    assert_eq!(health("dep"), "dependency-missing");
    assert_eq!(
        row(&listed, "dep")["missing_requirements"],
        json!(["clarp-plain-tool"])
    );
    assert_eq!(health("mine"), "modified");
    assert_eq!(row(&listed, "mine")["links"]["claude"], "modified");
    // Antigravity's folder never decides a skill's health.
    assert_eq!(health("agyown"), "healthy");
    assert_eq!(row(&listed, "agyown")["links"]["agy"], "modified");
    assert_eq!(health("nosrc"), "missing");
    let features = host.get("/server-info")["contract"]["features"].clone();
    assert_eq!(features["managed_skills"], 1);

    // The settings belong to a full-access caller.
    let (limited, _) = pair(&host, "Limited", "limited");
    for method in ["GET", "POST"] {
        let body = json!({ "skill_id": "opt2", "enabled": true });
        assert_eq!(
            host.ask_as(&limited, method, "/managed-skills", Some(&body))
                .0,
            403
        );
    }
    // Refusals in the reference's words; none changes a folder or the list.
    let before = machine.snapshot();
    let claude = machine.folder("claude");
    for (body, status, why) in [
        (json!([1]), 400, "bad json".to_owned()),
        (
            json!({}),
            400,
            "skill_id and boolean enabled are required".to_owned(),
        ),
        (
            json!({ "skill_id": "opt2", "enabled": 1 }),
            400,
            "skill_id and boolean enabled are required".to_owned(),
        ),
        (
            json!({ "skill_id": "  ", "enabled": true }),
            400,
            "skill_id and boolean enabled are required".to_owned(),
        ),
        (
            json!({ "skill_id": "nope", "enabled": true }),
            409,
            "unknown Clarp skill: nope".to_owned(),
        ),
        (
            json!({ "skill_id": "core1", "enabled": false }),
            409,
            "core Clarp skills cannot be disabled".to_owned(),
        ),
        (
            json!({ "skill_id": "nosrc", "enabled": true }),
            409,
            "skill source is missing: nosrc".to_owned(),
        ),
        (
            json!({ "skill_id": "mine", "enabled": true }),
            409,
            format!(
                "preserving non-Clarp skill at {}",
                claude.join("mine").display()
            ),
        ),
    ] {
        assert_eq!(
            host.ask("POST", "/managed-skills", Some(&body)),
            (status, json!({ "error": why })),
            "{body}"
        );
    }
    // A body of nothing is an empty object, as there.
    let required = json!({ "error": "skill_id and boolean enabled are required" });
    assert_eq!(host.ask("POST", "/managed-skills", None), (400, required));
    assert_eq!(machine.snapshot(), before);

    // A skill switched on is linked into every folder; Antigravity's own is left alone there.
    let on = host.post(
        "/managed-skills",
        json!({ "skill_id": " agyown ", "enabled": true }),
    );
    assert_eq!(
        on,
        json!({ "ok": true, "skill": { "id": "agyown", "pack": "optional",
                "description": "Antigravity has one", "enabled": true } })
    );
    assert_eq!(machine.leads("claude", "agyown"), ours("agyown"));
    assert_eq!(machine.leads("codex", "agyown"), ours("agyown"));
    assert!(!machine.folder("agy").join("agyown").is_symlink());
    assert_eq!(health_now(&host, "agyown"), "healthy");
    // Switched off: its links go, what is not Clarp's stays.
    let off = host.post(
        "/managed-skills",
        json!({ "skill_id": "opt1", "enabled": false }),
    );
    assert_eq!(off["skill"]["enabled"], false);
    for tool in ["claude", "codex", "agy"] {
        assert_eq!(machine.leads(tool, "opt1"), None, "{tool}");
    }
    assert_eq!(health_now(&host, "opt1"), "inactive");
    assert_eq!(state()["skills_off"], json!(["core1", "opt1", "opt2"]));
    host.post(
        "/managed-skills",
        json!({ "skill_id": "opt2", "enabled": true }),
    );
    assert_eq!(health_now(&host, "opt2"), "dependency-missing");
    assert_eq!(state()["skills_off"], json!(["core1", "opt1"]));
    // A core skill switched on again is linked again.
    let again = host.post(
        "/managed-skills",
        json!({ "skill_id": "core1", "enabled": true }),
    );
    assert_eq!(again["skill"]["id"], "core1");

    // A later manifest adds a skill and retires another. Recreate a link left for an opt-out.
    host.kill();
    let other_state = r#"{"skills":[],"skills_off":["art1"]}"#;
    std::fs::write(&machine.seed, other_state).unwrap();
    let manifest_path = tree.join("manifest.json");
    let mut manifest: Value =
        serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    let items = manifest["skills"].as_array_mut().unwrap();
    items.retain(|item| item["id"] != "dep");
    items.push(json!({"id": "new", "pack": "optional", "requires": ["clarp-absent-tool"]}));
    std::fs::write(manifest_path, manifest.to_string()).unwrap();
    skill(&tree, "new");
    link(&machine.folder("claude").join("opt1"), &tree.join("opt1"));
    std::fs::remove_file(machine.folder("claude").join("agyown")).unwrap();
    let host = machine.start(&place, Some("select"));
    let listed = host.get("/managed-skills");
    let enabled: Vec<&str> = listed["skills"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["enabled"] == true)
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        enabled,
        [
            "core1", "core2", "art1", "opt2", "mine", "agyown", "nosrc", "new"
        ]
    );
    assert_eq!(health_now(&host, "new"), "dependency-missing");
    for tool in ["claude", "codex", "agy"] {
        assert_eq!(machine.leads(tool, "new"), ours("new"), "{tool}");
        assert_eq!(machine.leads(tool, "dep"), None, "{tool}: retired");
    }
    assert_eq!(state()["skills_off"], json!(["opt1"]));
    assert_eq!(
        state()["skills"],
        json!([
            "agyown", "art1", "core1", "core2", "mine", "new", "nosrc", "opt2"
        ])
    );
    assert_eq!(
        machine.leads("claude", "agyown"),
        ours("agyown"),
        "linked again at the start"
    );
    assert_eq!(
        machine.leads("claude", "opt1"),
        None,
        "switched off stays off"
    );
    assert_eq!(machine.leads("claude", "art1"), ours("art1"));
    assert_eq!(std::fs::read_to_string(&machine.seed).unwrap(), other_state);
    // The older releases' trees are untouched by all of it.
    assert!(machine.older.join("core1/SKILL.md").is_file());
    assert!(machine.python.join("r0/skills/gone/SKILL.md").is_file());
    // Where Antigravity is not installed (its configuration folder is gone), its folder is
    // neither read nor written.
    std::fs::remove_dir_all(machine.home.join(".gemini")).unwrap();
    assert!(
        row(&host.get("/managed-skills"), "core1")["links"]
            .get("agy")
            .is_none()
    );
    host.post(
        "/managed-skills",
        json!({ "skill_id": "art1", "enabled": true }),
    );
    assert!(!machine.home.join(".gemini").exists());
    host.kill();
}

/// The links of Clarp's (those leading into `ours`) in `folders` whose skill does not read:
/// each must lead to a SKILL.md of one release, never to nothing.
fn unreadable(folders: &[PathBuf], ours: &[PathBuf]) -> Vec<String> {
    let mut wrong = Vec::new();
    for folder in folders {
        let Ok(entries) = std::fs::read_dir(folder) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(target) = std::fs::read_link(entry.path()) else {
                continue;
            };
            if ours.iter().any(|root| target.starts_with(root))
                && std::fs::read_to_string(entry.path().join("SKILL.md")).is_err()
            {
                wrong.push(format!(
                    "{} -> {}",
                    entry.path().display(),
                    target.display()
                ));
            }
        }
    }
    wrong
}

/// The first line a Host's journal said that holds these words.
fn said_line(host: &Host, words: &str) -> String {
    let heard = host.heard.lock().unwrap();
    heard
        .lines()
        .find(|line| line.contains(words))
        .unwrap()
        .to_owned()
}

/// What a Host's journal said of the time its reconciliation took, in ms.
fn reconciled_ms(host: &Host) -> u64 {
    let heard = host.heard.lock().unwrap();
    let after = heard
        .split("clarp-host: skills reconciled in ")
        .nth(1)
        .unwrap();
    after.split(" ms").next().unwrap().parse().unwrap()
}

/// Issue 935: a Host answers, streams and finishes its start while its skills are still being
/// reconciled; the reconciliation comes after, alone with the tools' links (a change asked for
/// meanwhile waits for it), and every link of Clarp's reads a skill of one release before,
/// during and after it, through a rollback cut off halfway and a reconciliation that failed and
/// was tried again. The install record names every release a link still leads into.
#[test]
fn the_skills_are_reconciled_beside_the_start_and_every_link_reads_throughout() {
    let place = Place::new("skills-beside");
    let machine = Machine::new(&place);
    let canonical = |path: &Path| std::fs::canonicalize(path).unwrap();
    let (r2, r1) = (canonical(&machine.tree), canonical(&machine.older));
    // The older release ships the same manifest: the bundle a rollback puts back.
    std::fs::copy(
        machine.tree.join("manifest.json"),
        machine.older.join("manifest.json"),
    )
    .unwrap();
    let folders: Vec<PathBuf> = ["claude", "codex", "agy"]
        .iter()
        .map(|tool| machine.folder(tool))
        .collect();
    let releases = machine
        .tree
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let ours = vec![
        canonical(&releases),
        canonical(&machine.python),
        releases,
        machine.python.clone(),
    ];
    let state_path = place.root.join("data/install.json");
    let state =
        || -> Value { serde_json::from_slice(&std::fs::read(&state_path).unwrap()).unwrap() };
    let release = |tree: &Path| tree.parent().unwrap().display().to_string();
    let on = json!([
        "agyown", "art1", "core1", "core2", "dep", "mine", "nosrc", "opt1", "opt2"
    ]);
    let hold = place.root.join("skills-held");
    let started = |host: &Host| host.get("/host/start")["stage"] == "started";

    // A fresh install: the start is done and the Host answers and streams with its skills held.
    std::fs::write(&hold, "").unwrap();
    let host = machine.start_of(&place, &machine.tree, Some("select"), Some(&hold));
    wait_for("the start done while the skills are held", || {
        started(&host)
    });
    let events = host.listen_at(&format!("/events?token={}", host.token));
    wait_for("the event stream", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|event| event["type"] == "server-version")
    });
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["managed_skills"],
        1
    );
    assert_eq!(reconciled(&host), 0);
    assert!(
        !state_path.exists(),
        "nothing recorded before the reconciliation"
    );
    // What the releases before left still reads.
    assert_eq!(
        machine.leads("codex", "core1"),
        Some(machine.older.join("core1"))
    );
    assert_eq!(unreadable(&folders, &ours), Vec::<String>::new());
    // Every link is sampled while the links move, until this is cleared: when the sampling is
    // done, or when an assertion below fails. Then the hold is let go too, so that the scope
    // waits neither on the probe nor on the change.
    let moving = std::sync::atomic::AtomicBool::new(true);
    struct Ends<'a>(&'a std::sync::atomic::AtomicBool, &'a Path);
    impl Drop for Ends<'_> {
        fn drop(&mut self) {
            self.0.store(false, std::sync::atomic::Ordering::Relaxed);
            let _ = std::fs::remove_file(self.1);
        }
    }
    std::thread::scope(|scope| {
        let _ends = Ends(&moving, &hold);
        // A change asked for meanwhile waits for the reconciliation, and is made after it.
        let change = scope.spawn(|| {
            host.ask(
                "POST",
                "/managed-skills",
                Some(&json!({ "skill_id": "opt2", "enabled": true })),
            )
        });
        std::thread::sleep(Duration::from_millis(400));
        assert!(
            !change.is_finished(),
            "a change ran beside the reconciliation"
        );
        assert_eq!(machine.leads("claude", "opt2"), None);
        let probe = scope.spawn(|| {
            let (mut samples, mut wrong) = (0, Vec::new());
            while moving.load(std::sync::atomic::Ordering::Relaxed) {
                wrong.extend(unreadable(&folders, &ours));
                samples += 1;
            }
            (samples, wrong)
        });
        std::fs::remove_file(&hold).unwrap();
        wait_for("the skills reconciled", || reconciled(&host) > 0);
        let (status, answer) = change.join().unwrap();
        assert_eq!(status, 200, "{answer}");
        moving.store(false, std::sync::atomic::Ordering::Relaxed);
        let (samples, wrong) = probe.join().unwrap();
        assert!(samples > 0);
        assert_eq!(wrong, Vec::<String>::new(), "{samples} samples");
    });
    // The monotonic marks of each step, on the journal line an ordinary start writes too.
    let line = said_line(&host, "clarp-host: skills reconciled in ");
    for step in [
        "shelf=",
        "lock=",
        "enter=",
        "held=",
        "manifest=",
        "choices=",
        "roots=",
        "before=",
        "intent=",
        "links=",
        "scan=",
        "write=",
        "sync=",
        "rename=",
        "back=",
    ] {
        assert!(line.contains(step), "{step} in {line}");
    }
    eprintln!("skills-beside: {line} (total {} ms)", reconciled_ms(&host));
    for tool in ["claude", "codex", "agy"] {
        assert_eq!(
            machine.leads(tool, "core1"),
            Some(r2.join("core1")),
            "{tool}"
        );
        assert_eq!(machine.leads(tool, "opt2"), Some(r2.join("opt2")), "{tool}");
    }
    assert_eq!(
        state(),
        json!({ "skills": on, "skills_off": ["core1"], "linked_releases": [release(&r2)] })
    );
    // What is not Clarp's stays where it was.
    assert_eq!(
        machine.leads("codex", "core2"),
        Some(place.root.join("elsewhere/core2"))
    );
    assert!(machine.folder("claude").join("mine/SKILL.md").is_file());
    assert!(!machine.folder("agy").join("agyown").is_symlink());
    assert_eq!(std::fs::read_to_string(&machine.seed).unwrap(), SEED);
    host.kill();

    // A rollback to the release before, cut off before its reconciliation: the links still
    // read this release, and the record is as it was.
    let recorded = std::fs::read(&state_path).unwrap();
    std::fs::write(&hold, "").unwrap();
    let host = machine.start_of(&place, &machine.older, Some("select"), Some(&hold));
    wait_for("the rollback's start done", || started(&host));
    assert_eq!(machine.leads("claude", "core1"), Some(r2.join("core1")));
    host.kill();
    std::fs::remove_file(&hold).unwrap();
    assert_eq!(std::fs::read(&state_path).unwrap(), recorded);
    assert_eq!(unreadable(&folders, &ours), Vec::<String>::new());

    // Started again, it cannot write one tool's folder: the skills stay whole in the release
    // they read, the record names it, and the reconciliation is tried again until it is done.
    use std::os::unix::fs::PermissionsExt;
    let codex = machine.folder("codex");
    std::fs::set_permissions(&codex, std::fs::Permissions::from_mode(0o555)).unwrap();
    let as_root = std::fs::write(codex.join(".probe"), "").is_ok();
    let host = machine.start_of(&place, &machine.older, Some("select"), None);
    if as_root {
        let _ = std::fs::remove_file(codex.join(".probe"));
        eprintln!("skills-beside: run as root, a folder cannot be made unwritable; not tried");
    } else {
        wait_for("a reconciliation in part", || {
            host.heard
                .lock()
                .unwrap()
                .contains("the skills were reconciled in part in ")
        });
        assert_eq!(reconciled(&host), 0);
        eprintln!(
            "skills-beside: {}",
            said_line(&host, "reconciled in part in ")
        );
        for tool in ["claude", "codex", "agy"] {
            assert_eq!(
                machine.leads(tool, "core1"),
                Some(r2.join("core1")),
                "{tool}"
            );
        }
        // A skill no folder refused moved whole; the record names both releases.
        assert_eq!(machine.leads("claude", "core2"), Some(r1.join("core2")));
        assert_eq!(
            state()["linked_releases"],
            json!([release(&r1), release(&r2)])
        );
        assert_eq!(unreadable(&folders, &ours), Vec::<String>::new());
        // Then it may write the folder but not list it: the links move, and the record still
        // names both releases, as where that folder's links lead is not known.
        let parts = || {
            host.heard
                .lock()
                .unwrap()
                .matches("the skills were reconciled in part in ")
                .count()
        };
        std::fs::set_permissions(&codex, std::fs::Permissions::from_mode(0o333)).unwrap();
        wait_for("the links moved in a folder not listed", || {
            machine.leads("codex", "core1") == Some(r1.join("core1"))
        });
        let seen = parts();
        wait_for("a whole try with the folder not listed", || parts() > seen);
        assert_eq!(reconciled(&host), 0);
        assert_eq!(machine.leads("claude", "core1"), Some(r1.join("core1")));
        assert_eq!(
            state()["linked_releases"],
            json!([release(&r1), release(&r2)])
        );
        // Then it may list the folder but not read a link in it: where those lead is not
        // known either, and the record still names both releases (counted as read, the links
        // it could not read would have left only r1).
        std::fs::set_permissions(&codex, std::fs::Permissions::from_mode(0o400)).unwrap();
        let seen = parts();
        wait_within(
            "a whole try with the links not readable",
            Duration::from_secs(120),
            || parts() > seen + 1,
        );
        assert_eq!(reconciled(&host), 0);
        assert_eq!(
            state()["linked_releases"],
            json!([release(&r1), release(&r2)])
        );
    }
    std::fs::set_permissions(&codex, std::fs::Permissions::from_mode(0o755)).unwrap();
    wait_within(
        "the reconciliation tried again",
        Duration::from_secs(120),
        || reconciled(&host) > 0,
    );
    eprintln!(
        "skills-beside: retried: {}",
        said_line(&host, "clarp-host: skills reconciled in ")
    );
    for tool in ["claude", "codex", "agy"] {
        assert_eq!(
            machine.leads(tool, "core1"),
            Some(r1.join("core1")),
            "{tool}"
        );
        assert_eq!(machine.leads(tool, "opt2"), Some(r1.join("opt2")), "{tool}");
        assert_eq!(machine.leads(tool, "opt1"), Some(r1.join("opt1")), "{tool}");
    }
    // The opt-out and the roots are kept, and the record says where every link leads now.
    assert_eq!(
        state(),
        json!({ "skills": on, "skills_off": ["core1"], "linked_releases": [release(&r1)] })
    );
    assert_eq!(
        machine.leads("codex", "core2"),
        Some(place.root.join("elsewhere/core2"))
    );
    assert!(machine.folder("claude").join("mine/SKILL.md").is_file());
    assert_eq!(unreadable(&folders, &ours), Vec::<String>::new());
    assert_eq!(
        host.get("/managed-skills")["skills"]
            .as_array()
            .unwrap()
            .len(),
        9
    );
    host.kill();
}

fn health_now(host: &Host, id: &str) -> String {
    row(&host.get("/managed-skills"), id)["health"]
        .as_str()
        .unwrap()
        .to_owned()
}

#[test]
fn a_host_that_does_not_keep_the_tools_reads_and_writes_none_of_their_skills() {
    let place = Place::new("skills-elsewhere");
    let machine = Machine::new(&place);
    let before = machine.snapshot();
    let refused = json!({
        "error": "This Host does not keep this machine's tools; the change to a skill is made on the Host that does.",
        "code": "skills_kept_elsewhere",
    });
    // Beside a Host that keeps the tools, and with no mode at all: a start writes nothing into
    // a tool's folder, the list is empty, and every change is refused in one sentence.
    for accounts in [Some("follow"), None] {
        let host = machine.start(&place, accounts);
        assert_eq!(machine.snapshot(), before, "{accounts:?}: the start wrote");
        assert_eq!(
            host.get("/managed-skills"),
            json!({ "skills": [] }),
            "{accounts:?}"
        );
        for body in [
            json!({ "skill_id": "opt2", "enabled": true }),
            json!({ "skill_id": "core1", "enabled": false }),
            json!({ "skill_id": "mine", "enabled": true }),
            json!({}),
            json!([1]),
        ] {
            assert_eq!(
                host.ask("POST", "/managed-skills", Some(&body)),
                (409, refused.clone()),
                "{accounts:?} {body}"
            );
        }
        let features = host.get("/server-info")["contract"]["features"].clone();
        assert!(
            features.get("managed_skills").is_none(),
            "{accounts:?}: {features}"
        );
        host.kill();
        assert_eq!(machine.snapshot(), before, "{accounts:?}: a change wrote");
    }
    // A Host alone on its machine that was installed without a tree of skills keeps none.
    let host = place.start_in(
        "fixture",
        &["--accounts", "select"],
        &[("HOME", machine.home.to_str().unwrap())],
    );
    assert_eq!(host.get("/managed-skills"), json!({ "skills": [] }));
    let (status, said) = host.ask(
        "POST",
        "/managed-skills",
        Some(&json!({ "skill_id": "opt2", "enabled": true })),
    );
    assert_eq!(
        (status, said),
        (
            409,
            json!({ "error": "This Host was installed without the skills it would keep.", "code": "skills_not_shipped" })
        )
    );
    assert!(
        host.get("/server-info")["contract"]["features"]
            .get("managed_skills")
            .is_none()
    );
    host.kill();
    assert_eq!(machine.snapshot(), before);
    assert!(!place.root.join("data/install.json").exists());
}

#[test]
fn the_install_tool_ships_the_skills_tree_and_names_it_without_touching_a_tools_folder() {
    let tool = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/install-host");
    let place = Place::new("skills-install");
    // The release's tree of skills, the test's own: a manifest, a skill with a script that
    // runs, and a skill with a reference beside its SKILL.md.
    let tree = place.root.join("release-skills");
    for (file, text) in [
        (
            "manifest.json",
            "{\"version\": 1, \"skills\": [{\"id\": \"alpha\", \"pack\": \"core\"}, {\"id\": \"beta\", \"pack\": \"optional\"}]}\n",
        ),
        ("alpha/SKILL.md", "---\nname: alpha\n---\nAlpha.\n"),
        ("beta/SKILL.md", "---\nname: beta\n---\nBeta.\n"),
        ("beta/references/notes.md", "Notes of beta.\n"),
    ] {
        std::fs::create_dir_all(tree.join(file).parent().unwrap()).unwrap();
        std::fs::write(tree.join(file), text).unwrap();
    }
    std::fs::create_dir_all(tree.join("alpha/scripts")).unwrap();
    program(
        &tree.join("alpha/scripts/run.sh"),
        "#!/bin/sh\necho alpha\n",
    );
    let from = place.root.join("from");
    std::fs::create_dir_all(&from).unwrap();
    for name in ["clarp-host", "clarp-shim"] {
        program(&from.join(name), "#!/bin/sh\n");
    }
    let sums = place
        .command("sha256sum")
        .args(["clarp-host", "clarp-shim"])
        .current_dir(&from)
        .output()
        .unwrap();
    std::fs::write(from.join("SHA256SUMS"), sums.stdout).unwrap();
    let home = place.root.join("home");
    // The Python Host that kept the tools before: its releases and its record of the chosen.
    let python = home.join(".local/share/clarp/releases");
    std::fs::create_dir_all(python.join("r0/skills")).unwrap();
    let chosen = home.join(".config/clarp/install.json");
    std::fs::create_dir_all(chosen.parent().unwrap()).unwrap();
    std::fs::write(&chosen, "{\"skills\": [\"clarp-media\"]}").unwrap();
    let share = home.join(".local/share/clarp-host");
    // A stand-in for systemctl: the Python Host's units are as the file `units` says, and
    // nothing else is answered.
    let units = place.root.join("units");
    let bin = place.root.join("bin");
    std::fs::create_dir_all(&bin).unwrap();
    program(
        &bin.join("systemctl"),
        &format!(
            "#!/bin/sh\nfor arg in \"$@\"; do last=\"$arg\"; done\ncase \" $* \" in\n  *\" is-active \"*)\n    state=\"$(sed -n \"s/^$last=//p\" '{}' 2>/dev/null)\"\n    echo \"${{state:-inactive}}\"\n    [ \"${{state:-inactive}}\" = active ] && exit 0 || exit 3 ;;\nesac\nexit 0\n",
            units.display()
        ),
    );
    let path = format!("{}:{}", bin.display(), sealed::PATH);
    let run_from = |commit: &str, skills: &Path| -> std::process::Output {
        std::fs::write(from.join("COMMIT"), format!("{commit}\n")).unwrap();
        place
            .command("bash")
            .arg(&tool)
            .args([
                "--from",
                from.to_str().unwrap(),
                "--files-only",
                "--accounts",
                "select",
            ])
            .args(["--skills-from", skills.to_str().unwrap()])
            .env("HOME", &home)
            .env("PATH", &path)
            .env("XDG_CONFIG_HOME", home.join(".config"))
            .env("XDG_DATA_HOME", home.join(".local/share"))
            .output()
            .unwrap()
    };
    let run = |commit: &str| run_from(commit, &tree);
    // While the Python Host of the machine runs, with or without a selector of accounts, this
    // Host does not keep the machine's tools: select is refused, and nothing is written.
    let selector = home.join(".config/clarp/config.toml");
    for (state, with_selector) in [
        ("clarp.service=active\n", false),
        ("clarp-runtime.service=activating\n", false),
        ("clarp.service=active\n", true),
    ] {
        std::fs::write(&units, state).unwrap();
        if with_selector {
            std::fs::write(
                &selector,
                "[agents]\nclaude_account_switch_command = \"x\"\n",
            )
            .unwrap();
        }
        let refused = run("aaaaaaaaaaaa1111");
        let said = String::from_utf8_lossy(&refused.stderr);
        assert_eq!(refused.status.code(), Some(2), "{state}: {said}");
        assert!(
            said.contains("--accounts select: the Python Host of this machine runs."),
            "{said}"
        );
        assert!(
            said.contains("Stop that Host first, or use --accounts follow beside it."),
            "{said}"
        );
        assert!(!share.exists(), "{state}");
        assert!(!home.join(".config/clarp-host").exists(), "{state}");
        assert!(!home.join(".config/systemd").exists(), "{state}");
        let _ = std::fs::remove_file(&selector);
    }
    // A tree named without a manifest is refused, before anything is written.
    std::fs::write(&units, "").unwrap();
    let refused = run_from("aaaaaaaaaaaa1111", &tree.join("alpha"));
    let said = String::from_utf8_lossy(&refused.stderr);
    assert_eq!(refused.status.code(), Some(2), "{said}");
    assert!(
        said.contains("--skills-from") && said.contains("no manifest.json there"),
        "{said}"
    );
    assert!(!share.exists() && !home.join(".config/clarp-host").exists());
    // Stopped, it is taken.
    std::fs::write(
        &units,
        "clarp.service=inactive\nclarp-runtime.service=failed\n",
    )
    .unwrap();
    // The Host's data is where a Host of this place keeps it, for the Hosts started below.
    std::fs::create_dir_all(place.root.join("data")).unwrap();
    std::fs::create_dir_all(&share).unwrap();
    std::os::unix::fs::symlink(place.root.join("data"), share.join("data")).unwrap();
    let install = |commit: &str| -> String {
        let done = run(commit);
        assert!(
            done.status.success(),
            "{}",
            String::from_utf8_lossy(&done.stderr)
        );
        let unit =
            std::fs::read_to_string(home.join(".config/systemd/user/clarp-host.service")).unwrap();
        unit.lines()
            .find(|line| line.starts_with("ExecStart="))
            .unwrap()
            .to_owned()
    };
    let started = install("aaaaaaaaaaaa1111");
    let release = share.join("releases/aaaaaaaaaaaa/skills");
    let named = format!(
        " --skills {} --skills-other-releases {} --skills-chosen-before {}",
        release.display(),
        python.display(),
        chosen.display()
    );
    assert!(started.contains(&named), "{started}");
    // The tree is the one it was given, whole and as it is: every file with its bytes, and a
    // script still runs.
    fn files(root: &Path, at: &Path, into: &mut Vec<(String, Vec<u8>, bool)>) {
        use std::os::unix::fs::PermissionsExt;
        let mut entries: Vec<PathBuf> = std::fs::read_dir(at)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        entries.sort();
        for entry in entries {
            if entry.is_dir() {
                files(root, &entry, into);
            } else {
                let runs = std::fs::metadata(&entry).unwrap().permissions().mode() & 0o100 != 0;
                let name = entry.strip_prefix(root).unwrap().display().to_string();
                into.push((name, std::fs::read(&entry).unwrap(), runs));
            }
        }
    }
    let (mut given, mut shipped) = (Vec::new(), Vec::new());
    files(&tree, &tree, &mut given);
    files(&release, &release, &mut shipped);
    assert_eq!(given.len(), 5);
    assert_eq!(shipped, given);
    assert!(
        shipped
            .iter()
            .any(|(name, _, runs)| name == "alpha/scripts/run.sh" && *runs)
    );
    // A newer version gets a place of its own. Where no Host has said yet where the tools'
    // links lead, none is taken away (issue 935).
    install("bbbbbbbbbbbb2222");
    install("cccccccccccc3333");
    let kept = || -> Vec<String> {
        let mut kept: Vec<String> = std::fs::read_dir(share.join("releases"))
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        kept.sort();
        kept
    };
    assert_eq!(kept(), ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]);
    // Nothing of a tool's skill folder is made or touched, and the other Host's record is read only.
    for folder in [".claude", ".codex", ".gemini"] {
        assert!(!home.join(folder).exists(), "{folder}");
    }
    assert_eq!(
        std::fs::read_to_string(&chosen).unwrap(),
        "{\"skills\": [\"clarp-media\"]}"
    );

    // The links lead into b, as its Host left them. The Host of c moves them and is cut off
    // before it has settled its record: the record names b and c, written before any link moved.
    let releases = std::fs::canonicalize(share.join("releases")).unwrap();
    let at = |version: &str| releases.join(version).join("skills");
    let claude = home.join(".claude/skills");
    for id in ["alpha", "beta"] {
        link(&claude.join(id), &at("bbbbbbbbbbbb").join(id));
    }
    let record = share.join("data/install.json");
    let named = || -> Value {
        serde_json::from_slice::<Value>(&std::fs::read(&record).unwrap()).unwrap()
            ["linked_releases"]
            .clone()
    };
    let release_of = |version: &str| releases.join(version).display().to_string();
    let reads = |version: &str| {
        for folder in [".claude/skills", ".codex/skills"] {
            for id in ["alpha", "beta"] {
                let place = home.join(folder).join(id);
                assert_eq!(
                    std::fs::read_link(&place).unwrap(),
                    at(version).join(id),
                    "{folder}/{id}"
                );
                assert!(std::fs::read_to_string(place.join("SKILL.md")).is_ok());
            }
        }
    };
    let host_of = |version: &str, held: Option<&Path>| -> Host {
        let tree = share.join("releases").join(version).join("skills");
        let args = ["--skills", tree.to_str().unwrap(), "--accounts", "select"];
        let mut world = vec![("HOME", home.to_str().unwrap()), ("PATH", sealed::PATH)];
        if let Some(held) = held {
            world.push(("CLARP_TEST_SKILLS_SAVE_HELD_WHILE", held.to_str().unwrap()));
        }
        let without = [
            "CODEX_HOME",
            "CLARP_CLAUDE_SKILLS",
            "CLARP_CODEX_SKILLS",
            "CLARP_AGY_SKILLS",
        ];
        place.start_without("fixture", &args, &world, &without)
    };
    let held = place.root.join("save-held");
    std::fs::write(&held, "").unwrap();
    let host = host_of("cccccccccccc", Some(&held));
    wait_for("the links moved into c", || {
        ["alpha", "beta"].iter().all(|id| {
            std::fs::read_link(home.join(".codex/skills").join(id))
                .is_ok_and(|to| to == at("cccccccccccc").join(id))
        })
    });
    host.kill();
    std::fs::remove_file(&held).unwrap();
    reads("cccccccccccc");
    let both = json!([release_of("bbbbbbbbbbbb"), release_of("cccccccccccc")]);
    assert_eq!(named(), both);
    // Two installs of files only follow: c is neither the one before nor steady by then, and
    // stays, as b does, while the record names them.
    install("dddddddddddd4444");
    install("eeeeeeeeeeee5555");
    assert_eq!(
        kept(),
        [
            "bbbbbbbbbbbb",
            "cccccccccccc",
            "dddddddddddd",
            "eeeeeeeeeeee"
        ]
    );
    reads("cccccccccccc");
    assert_eq!(named(), both, "this tool never writes the record");
    // The Host of e settles its links and its record: then b and c go.
    let host = host_of("eeeeeeeeeeee", None);
    wait_for("the skills reconciled", || reconciled(&host) > 0);
    host.kill();
    reads("eeeeeeeeeeee");
    assert_eq!(named(), json!([release_of("eeeeeeeeeeee")]));
    install("ffffffffffff6666");
    assert_eq!(kept(), ["eeeeeeeeeeee", "ffffffffffff"]);
    // A record of a Host from before names none: every release stays.
    std::fs::write(&record, "{\"skills\": [], \"skills_off\": []}\n").unwrap();
    install("gggggggggggg7777");
    assert_eq!(kept(), ["eeeeeeeeeeee", "ffffffffffff", "gggggggggggg"]);
    assert_eq!(
        std::fs::read_to_string(&record).unwrap(),
        "{\"skills\": [], \"skills_off\": []}\n"
    );
}

/// Issue 935: the reconciliation of a machine shaped as the owner's (43 shipped skills linked
/// from a release before into two folders, one of them reached by Claude and Codex both through
/// links to a tree of the owner's beside 27 skills of its own, and an install record already
/// there), with each step's time on its journal line: what the steps cost when nothing else
/// holds the disk.
#[test]
fn a_machine_shaped_reconciliation_says_what_each_step_took() {
    let place = Place::new("skills-shaped");
    let root = &place.root;
    let home = root.join("home");
    let (before, tree) = (
        root.join("share/releases/before/skills"),
        root.join("share/releases/now/skills"),
    );
    let ids: Vec<String> = (0..45).map(|n| format!("clarp-skill-{n:02}")).collect();
    let items: Vec<Value> = ids
        .iter()
        .map(|id| json!({ "id": id, "pack": "optional", "description": id }))
        .collect();
    for at in [&before, &tree] {
        std::fs::create_dir_all(at).unwrap();
        std::fs::write(
            at.join("manifest.json"),
            json!({ "version": 1, "skills": items }).to_string(),
        )
        .unwrap();
        for id in &ids {
            skill(at, id);
        }
    }
    // Two skills were switched off; the other 43 lead into the release before.
    let off = ["clarp-skill-07", "clarp-skill-31"];
    let on: Vec<&String> = ids
        .iter()
        .filter(|id| !off.contains(&id.as_str()))
        .collect();
    let dots = root.join("dots/skills");
    let agy = home.join(".gemini/config/skills");
    for id in &on {
        link(&dots.join(id), &before.join(id));
        link(&agy.join(id), &before.join(id));
    }
    for n in 0..27 {
        let own = root.join(format!("own/skill-{n:02}"));
        skill(own.parent().unwrap(), &format!("skill-{n:02}"));
        link(&dots.join(format!("skill-{n:02}")), &own);
    }
    link(&home.join(".claude/skills"), &dots);
    link(&home.join(".codex/skills"), &dots);
    std::fs::create_dir_all(root.join("data")).unwrap();
    let chosen = json!({ "skills": on, "skills_off": off });
    std::fs::write(root.join("data/install.json"), chosen.to_string()).unwrap();

    let args = ["--skills", tree.to_str().unwrap(), "--accounts", "select"];
    let world = [("HOME", home.to_str().unwrap()), ("PATH", sealed::PATH)];
    let without = [
        "CODEX_HOME",
        "CLARP_CLAUDE_SKILLS",
        "CLARP_CODEX_SKILLS",
        "CLARP_AGY_SKILLS",
    ];
    let host = place.start_without("fixture", &args, &world, &without);
    wait_for("the skills reconciled", || reconciled(&host) > 0);
    let line = said_line(&host, "clarp-host: skills reconciled in ");
    assert!(
        line.contains(
            ": 86 links put in place, 0 taken away; the links lead into 1 releases; steps ms: lock="
        ),
        "{line}"
    );
    eprintln!("skills-shaped: {line}");
    let now = std::fs::canonicalize(&tree).unwrap();
    for id in &on {
        assert_eq!(std::fs::read_link(dots.join(id)).unwrap(), now.join(id));
        assert_eq!(std::fs::read_link(agy.join(id)).unwrap(), now.join(id));
    }
    for id in off {
        assert!(!dots.join(id).exists() && !agy.join(id).exists());
    }
    for n in 0..27 {
        let own = root.join(format!("own/skill-{n:02}"));
        assert_eq!(
            std::fs::read_link(dots.join(format!("skill-{n:02}"))).unwrap(),
            own
        );
    }
    host.kill();
}
