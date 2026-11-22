//! A cut-over carries the running jobs of an agent that moved, as they stood on the Python Host,
//! so that a worker that keeps running is still the run its job knows.
use super::*;
use rusqlite::{Connection, params};

const PYTHON_JOBS: &str = "CREATE TABLE background_jobs (
    job_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other', title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    cancelled_at INTEGER, metadata_json TEXT NOT NULL DEFAULT '{}', heartbeat_at INTEGER,
    heartbeat_timeout_ms INTEGER NOT NULL DEFAULT 600000,
    heartbeat_source TEXT NOT NULL DEFAULT 'unknown', worker_pid INTEGER,
    worker_start_token TEXT NOT NULL DEFAULT '', terminal_at INTEGER,
    terminal_reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 1, owner_kind TEXT NOT NULL DEFAULT 'agent',
    computer_id TEXT, progress_text TEXT NOT NULL DEFAULT '', progress_at INTEGER,
    log_path TEXT NOT NULL DEFAULT '', worker_cwd TEXT NOT NULL DEFAULT '',
    started_trace_id TEXT NOT NULL DEFAULT '');";

/// A Python job row: id, agent, session, title, status, heard at, worker pid and its start
/// token, run, and when it ended.
type PythonRow<'a> = (
    &'a str,
    &'a str,
    &'a str,
    &'a str,
    &'a str,
    i64,
    u32,
    &'a str,
    i64,
    Option<i64>,
);

#[test]
fn a_running_python_job_is_carried_so_its_live_worker_is_still_its_run() {
    let place = Place::new("import-jobs");
    let dir = place.root.clone();
    let host = place.start();
    let session = host.new_agent("Mover");
    let agent_id = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|agent| agent["session"] == session.as_str())
        .unwrap()["agent_id"]
        .as_str()
        .unwrap()
        .to_string();
    // A job this Host already has under an id the Python Host also has.
    let (code, handle, _) = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-agent-bg",
            &session,
            "job-upsert",
            "kept-1",
            "watch",
            "Rust title",
        ],
    );
    assert_eq!((code, handle.trim()), (0, "bg1:1:kept-1"));
    drop(host);

    // The live worker, and one that has ended.
    let mut live = Command::new("sleep").arg("300").spawn().unwrap();
    let live_pid = live.id();
    let live_token = clarp_host::process::start_token(live_pid);
    assert!(!live_token.is_empty());
    let mut gone = Command::new("true").spawn().unwrap();
    let gone_pid = gone.id();
    gone.wait().unwrap();

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let source = place.root.join("state.sqlite");
    let python = Connection::open(&source).unwrap();
    python.execute_batch(PYTHON_JOBS).unwrap();
    let insert = "INSERT INTO background_jobs(job_id, agent_id, session, kind, title, detail,
        status, started_at, updated_at, metadata_json, heartbeat_at, heartbeat_source, worker_pid,
        worker_start_token, generation, progress_text, progress_at, log_path, worker_cwd,
        started_trace_id, terminal_at)
        VALUES (?1, ?2, ?3, 'watch', ?4, 'detail', ?5, 1000, 2000, '{\"port\":7777}', ?6,
        'worker_heartbeat', ?7, ?8, ?9, 'tunnel up', 1500, '/var/tmp/tunnel.log', '/var/tmp',
        'trace-1', ?10)";
    let gone_token = format!("{}:1", live_token.split(':').next().unwrap());
    let rows: [PythonRow<'_>; 5] = [
        (
            "carried-1",
            &agent_id,
            &session,
            "Tunnel",
            "running",
            now,
            live_pid,
            &live_token,
            3,
            None,
        ),
        (
            "dead-1",
            &agent_id,
            &session,
            "Dead",
            "running",
            now,
            gone_pid,
            &gone_token,
            1,
            None,
        ),
        (
            "kept-1",
            &agent_id,
            &session,
            "Python title",
            "active",
            now,
            live_pid,
            &live_token,
            5,
            None,
        ),
        (
            "stranger-1",
            "python-only",
            "other",
            "Elsewhere",
            "running",
            0,
            0,
            "",
            1,
            None,
        ),
        (
            "over-1",
            &agent_id,
            &session,
            "Over",
            "succeeded",
            now,
            live_pid,
            &live_token,
            1,
            Some(now),
        ),
    ];
    for (id, agent, owner, title, status, heard, pid, token, generation, ended) in rows {
        python
            .execute(
                insert,
                params![
                    id, agent, owner, title, status, heard, pid, token, generation, ended
                ],
            )
            .unwrap();
    }
    drop(python);
    let before = std::fs::read(&source).unwrap();

    let data = place.root.join("data");
    let import = |more: &[&str]| {
        let out = place
            .host_command()
            .args(["import", "--jobs", "--from-db"])
            .arg(&source)
            .arg("--data")
            .arg(&data)
            .args(more)
            .ended();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    };
    let has = |said: &str, line: &str| {
        assert!(
            said.lines().any(|l| l.starts_with(line)),
            "{line:?} not in:\n{said}"
        );
    };
    for _ in 0..2 {
        let said = import(&["--dry-run"]);
        has(
            &said,
            &format!("would carry {session} carried-1 (run 3, worker {live_pid})"),
        );
        has(
            &said,
            &format!("skipped {session} dead-1: its worker process {gone_pid} is gone"),
        );
        has(&said, &format!("already there {session} kept-1"));
        has(
            &said,
            "skipped other stranger-1: its agent is not on the Rust Host (no worker named",
        );
        assert!(!said.contains("over-1"), "{said}");
        assert!(!said.contains(&live_token), "{said}");
        has(&said, "1 would be carried, 2 skipped, 1 already there");
    }
    let said = import(&[]);
    has(
        &said,
        &format!("carried {session} carried-1 (run 3, worker {live_pid})"),
    );
    has(&said, "1 carried, 2 skipped, 1 already there");
    let said = import(&[]);
    has(&said, &format!("already there {session} carried-1"));
    has(&said, "0 carried, 2 skipped, 2 already there");
    assert_eq!(std::fs::read(&source).unwrap(), before);

    let host = place.start();
    let active = |worker: Option<u32>, handle: &str| {
        tool(
            &host,
            &dir,
            worker,
            &["clarp-agent-bg", &session, "job-active", handle],
        )
        .0
    };
    // The live worker is the run its job knows: it goes on, and its heartbeats land.
    assert_eq!(active(Some(live_pid), "bg1:3:carried-1"), 0);
    assert_eq!(
        tool(
            &host,
            &dir,
            Some(live_pid),
            &[
                "clarp-agent-bg",
                &session,
                "job-heartbeat",
                "bg1:3:carried-1"
            ]
        )
        .0,
        0
    );
    // Another process is not.
    assert_eq!(active(Some(gone_pid), "bg1:3:carried-1"), 1);
    let detail = host.get("/background-jobs/carried-1");
    assert_eq!(detail["handle"], "bg1:3:carried-1", "{detail}");
    assert_eq!(detail["owner_session"], session.as_str());
    assert_eq!(detail["job"]["title"], "Tunnel", "{detail}");
    assert_eq!(detail["job"]["status"], "running", "{detail}");
    assert_eq!(detail["started_trace_id"], "trace-1");
    assert_eq!(detail["progress"]["text"], "tunnel up");
    // The job that was already here is as it was; the one whose worker was gone never came.
    assert_eq!(active(None, "bg1:1:kept-1"), 0);
    let kept = host.get("/background-jobs/kept-1");
    assert_eq!(kept["job"]["title"], "Rust title", "{kept}");
    assert_eq!(active(Some(gone_pid), "bg1:1:dead-1"), 2);
    drop(host);
    let _ = live.kill();
    let _ = live.wait();
}
