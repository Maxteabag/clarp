//! Port of desktop/tests/tst_workspace_context.cpp (uses a real Git repo).

use std::process::Command;

use clarp_core::workspace::WorkspaceContext;

#[test]
fn ordinary_directory_and_real_git_worktree() {
    let temp = std::env::temp_dir().join(format!("clarp-workspace-{}", std::process::id()));
    let repo = temp.join("project");
    let linked = temp.join("feature-view");
    std::fs::create_dir_all(&repo).unwrap();
    let git = |args: &[&str]| {
        Command::new("git").current_dir(&repo).args(["-c", "core.hooksPath=/dev/null"]).args(args)
            .output().map(|o| o.status.success()).unwrap_or(false)
    };
    assert!(git(&["init", "-q"]));
    assert!(git(&["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                  "commit", "-q", "--allow-empty", "-m", "fixture"]));
    assert!(git(&["worktree", "add", "-q", "--detach", linked.to_str().unwrap()]));
    std::fs::create_dir_all(linked.join("src")).unwrap();
    let mut context = WorkspaceContext::default();
    let plain = context.describe(temp.to_str().unwrap(), true);
    assert_eq!(plain["kind"], "directory");
    let main = context.describe(repo.to_str().unwrap(), true);
    assert_eq!(main["kind"], "repo");
    assert_eq!(main["repository"], "project");
    let branch = context.describe(linked.join("src").to_str().unwrap(), true);
    assert_eq!(branch["kind"], "worktree");
    assert_eq!(branch["label"], "project / feature-view / src");
    assert_eq!(branch["root"], linked.to_str().unwrap());
    let remote = context.describe(linked.to_str().unwrap(), false);
    assert_eq!(remote["kind"], "directory");
    assert_eq!(remote["verified"], false);
    assert!(!remote.contains_key("repository"));
    std::fs::remove_dir_all(temp).unwrap();
}
