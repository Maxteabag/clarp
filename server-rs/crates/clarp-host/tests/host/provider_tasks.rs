//! Background tasks observed during a managed turn.
use super::*;

#[test]
fn the_host_observes_native_bash_jobs_and_only_their_launching_attempt_ends_them() {
    const TOKEN: &str = "fixture-admin";
    let place = Place::new("provider-observer");
    let cwd = place.root.join("work");
    let sid = "a10bafe2-20cd-4d3e-b884-8dacf57c5aa4";
    let project = place
        .root
        .join(".claude/projects")
        .join(clarp_backend::past::claude_project(cwd.to_str().unwrap()));
    std::fs::create_dir_all(&project).unwrap();
    let parent = project.join(format!("{sid}.jsonl"));
    let launch = |tool: &str, time: &str| {
        json!({"type":"assistant","timestamp":time,"sessionId":sid,
        "message":{"role":"assistant","content":[{"type":"tool_use","name":"Bash","id":tool,"input":{"run_in_background":true,"description":"Check password=fixture"}}]}})
    };
    let receipt = |tool: &str, time: &str| {
        json!({"type":"user","timestamp":time,"sessionId":sid,
        "message":{"content":[{"type":"tool_result","tool_use_id":tool}]},"toolUseResult":{"backgroundTaskId":"fixture-child"}})
    };
    std::fs::write(
        &parent,
        format!("{}\n", launch("pre-binding", "2000-01-01T00:00:00Z")),
    )
    .unwrap();
    let append = |row: &Value| {
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .open(&parent)
            .unwrap();
        writeln!(file, "{row}").unwrap();
    };
    let fake = place.root.join("fixture-claude");
    let release = place.root.join("release");
    let init = json!({"type":"system","subtype":"init","session_id":sid});
    let done = json!({"type":"result","subtype":"success","session_id":sid,"result":"Done."});
    // The detached fixture acknowledges challenges even after its turn and
    // its Host end. A removed test place also releases the fixture.
    let detached = place.root.join("detached");
    program(
        &detached,
        r#"#!/bin/sh
cd "$1" || exit 1
while [ -d "$1" ] && [ ! -f detached-end ]; do
    if [ -f challenge ]; then mv challenge answer; fi
    sleep 0.05
done
"#,
    );
    // This stand-in calls the hook named in the real launch settings.
    // It never executes the updated Bash command.
    let probe = place.root.join("call-hook");
    program(
        &probe,
        r#"#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
settings=json.loads(sys.argv[sys.argv.index('--settings')+1])
hook=settings['hooks']['PreToolUse'][0]
assert set(settings)=={'hooks'} and set(settings['hooks'])=={'PreToolUse'}
assert hook['matcher']=='Bash' and len(hook['hooks'])==1
command=hook['hooks'][0]['command']
def invoke(data):
    result=subprocess.run(command,shell=True,input=data,stdout=subprocess.PIPE,timeout=4)
    assert result.returncode==0
    return result.stdout
payload={'session_id':'a10bafe2-20cd-4d3e-b884-8dacf57c5aa4','tool_use_id':'managed','tool_name':'Bash','tool_input':{'run_in_background':True,'command':'fixture'}}
encoded=json.dumps(payload).encode()
assert json.loads(invoke(encoded.ljust(65536)))['hookSpecificOutput']['updatedInput']['run_in_background'] is True
for invalid in (b'{',encoded.ljust(65537),json.dumps(dict(payload,tool_name='Read')).encode()):
    assert invoke(invalid)==b''
reply=json.loads(invoke(json.dumps(payload).encode()))['hookSpecificOutput']
assert set(reply)=={'updatedInput','additionalContext','hookEventName'}
pathlib.Path(__file__).with_name('hook-reply').write_text(json.dumps(reply))
"#,
    );
    program(
        &fake,
        &format!(
            "#!/bin/sh\nread -r request\n'{}' \"$@\" || exit 1\nsetsid '{}' '{}' </dev/null >/dev/null 2>&1 &\ntouch '{}'\nprintf '%s\\n' '{init}'\nwhile [ -d '{}' ] && [ ! -f '{}' ]; do sleep 0.05; done\nprintf '%s\\n' '{done}'\n",
            probe.display(),
            detached.display(),
            place.root.display(),
            place.root.join("started").display(),
            place.root.display(),
            release.display()
        ),
    );
    let env = [
        ("HOME", place.root.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", fake.to_str().unwrap()),
    ];
    let flags = ["--janitors-checked-every-ms", "100"];
    let host = place.start_in(TOKEN, &flags, &env);
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["provider_background_tasks"],
        24
    );
    let made = host.post(
        "/agents",
        json!({"backend":"claude","name":"Fixture","cwd":cwd,"resume_session_id":sid}),
    );
    let session = made["session"].as_str().unwrap();
    let job = |host: &Host, tool: &str| {
        host.get("/background-jobs")["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|j| j["session"] == session && j["metadata"]["tool_use_id"] == tool)
            .cloned()
            .unwrap_or(Value::Null)
    };
    // The bound native runtime can receive evidence without a shim. Its
    // earlier history and uncorrelated tool results cannot create jobs.
    let stamp = || clarp_host::wire::iso(now_ms() + 1);
    append(&receipt("unclaimed", &stamp()));
    append(&launch("terminal", &stamp()));
    append(&receipt("terminal", &stamp()));
    wait_for("the idle binding sees a native receipt", || {
        job(&host, "terminal")["status"] == "running"
    });
    assert!(job(&host, "pre-binding").is_null());
    assert!(job(&host, "unclaimed").is_null());
    // A managed turn has a separate launch identity. Only its unfinished
    // tasks stop at its exit, even when the Host restarts during that turn.
    host.send(session, "Run the fixture", "native-turn");
    wait_for("the stand-in received the turn", || {
        place.root.join("started").is_file()
    });
    let time = stamp();
    append(&launch("managed", &time));
    append(&receipt("managed", &time));
    wait_for("the running managed task reaches the registry", || {
        job(&host, "managed")["status"] == "running"
    });
    let hook: Value =
        serde_json::from_slice(&std::fs::read(place.root.join("hook-reply")).unwrap()).unwrap();
    assert_eq!(hook["hookEventName"], "PreToolUse");
    assert!(
        hook["additionalContext"]
            .as_str()
            .unwrap()
            .contains("run_detached_job.sh --goal")
    );
    let amended = hook["updatedInput"]["command"].as_str().unwrap();
    let origin = amended
        .strip_prefix("export CLARP_BACKGROUND_ORIGIN='")
        .unwrap()
        .strip_suffix("'\nfixture")
        .unwrap();
    let origin_json: Value = serde_json::from_str(origin).unwrap();
    assert_eq!(origin_json["agent_id"], job(&host, "managed")["agent_id"]);
    assert_eq!(origin_json["native_session_id"], sid);
    assert_eq!(origin_json["tool_use_id"], "managed");
    let shown = job(&host, "managed");
    assert_eq!(shown["generation"], 1);
    assert_eq!(shown["kind"], "provider-task");
    assert_eq!(shown["heartbeat_source"], "provider_event");
    assert!(shown["heartbeat_at"].is_null() && shown["worker_pid"].is_null());
    assert_eq!(shown["worker_freshness"], "unknown");
    assert_eq!(shown["can_cancel"], false);
    assert_eq!(shown["title"], "Check password=[redacted]");
    let path = format!("/background-jobs/{}", shown["job_id"].as_str().unwrap());
    let detail = host.get(&path);
    assert_eq!(detail["job"]["revision"], shown["revision"]);
    assert_eq!(detail["timeline"].as_array().unwrap().len(), 2);
    // A limited device can inspect canonical state and progress, but the
    // provider's output path and bytes are withheld before any file read.
    let (limited, _) = pair(&host, "Task observer", "limited");
    let (status, restricted) = host.ask_as(&limited, "GET", &path, None);
    assert_eq!(status, 200);
    assert_eq!(restricted["timeline"], detail["timeline"]);
    assert_eq!(restricted["progress"], detail["progress"]);
    assert_eq!(
        restricted["log"],
        json!({"path":"","available":false,"reason":"forbidden","text":"","size":0,"truncated":false})
    );
    // A stale generation and an unsupported cancellation are separate
    // refusals and leave the job and its timeline at the same revision.
    let (status, stale) = host.ask("DELETE", &format!("{path}?expected_generation=2"), None);
    assert_eq!(status, 409);
    assert_eq!(stale["error"], "generation_mismatch");
    assert_eq!(stale["expected_generation"], 2);
    assert_eq!(stale["current_generation"], 1);
    let (status, refusal) = host.ask("DELETE", &path, None);
    assert_eq!(status, 409);
    assert_eq!(refusal["error"], "provider_task_cancel_unsupported");
    assert_eq!(refusal["changed"], false);
    assert_eq!(refusal["job_handle"], detail["handle"]);
    assert_eq!(refusal["job"]["revision"], shown["revision"]);
    assert_eq!(host.get(&path)["timeline"], detail["timeline"]);
    // The origin exported by the hook links an explicit registration to
    // exactly this task. Detail stays canonical while live clients remove
    // the superseded row before they receive the new worker's event.
    let events = host.listen_at(&format!("/events?token={TOKEN}"));
    wait_for(
        "the authenticated subscriber receives its initial roster",
        || {
            events
                .lock()
                .unwrap()
                .iter()
                .any(|event| event["type"] == "agent-roster")
        },
    );
    let register = |host: &Host, raw: &str| {
        let result = place
            .host_command()
            .args([
                "tool",
                "clarp-agent-bg",
                session,
                "job-upsert",
                "linked-worker",
                "fixture",
                "Explicit job",
            ])
            .env("HOME", &place.root)
            .env("CLARP_HOST_ADDR", &host.address)
            .env("CLARP_HOST_TOKEN", &host.token)
            .env("CLARP_BACKGROUND_ORIGIN", raw)
            .current_dir(&cwd)
            .ended();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    };
    let mut invalid = origin_json.clone();
    invalid["agent_id"] = json!("someone-else");
    register(&host, &invalid.to_string());
    assert!(
        host.get("/background-jobs/linked-worker")["job"]["metadata"]
            .get("tool_use_id")
            .is_none()
    );
    register(&host, origin);
    assert_eq!(
        host.get("/background-jobs/linked-worker")["job"]["metadata"]["tool_use_id"],
        "managed"
    );
    let event_id = shown["job_id"].as_str().unwrap();
    wait_for(
        "the registration follows its superseded native event",
        || {
            let kept = events.lock().unwrap();
            kept.iter().any(|e| {
                e["job_id"] == "linked-worker" && e["job"]["metadata"]["tool_use_id"] == "managed"
            })
        },
    );
    let kept = events.lock().unwrap();
    let mirrored = kept
        .iter()
        .position(|e| e["job_id"] == event_id && e["status"] == "superseded")
        .unwrap();
    let explicit = kept
        .iter()
        .position(|e| {
            e["job_id"] == "linked-worker" && e["job"]["metadata"]["tool_use_id"] == "managed"
        })
        .unwrap();
    assert!(mirrored < explicit);
    let native_revision = kept[mirrored]["change_revision"].as_u64().unwrap();
    let reg_revision = kept[explicit]["change_revision"].as_u64().unwrap();
    assert!(
        shown["revision"].as_u64().unwrap() < native_revision && native_revision < reg_revision
    );
    assert_eq!(kept[mirrored]["job"]["projection_only"], true);
    assert_eq!(kept[mirrored]["job"]["replaced_by_job_id"], "linked-worker");
    drop(kept);
    let visible = |host: &Host| {
        host.get("/background-jobs")["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|j| j["job_id"] == event_id)
    };
    assert!(!visible(&host));
    assert_eq!(host.get(&path)["job"]["status"], "running");
    host.kill();
    std::fs::write(&release, "end").unwrap();
    let host = place.start_in(TOKEN, &flags, &env);
    assert!(!visible(&host));
    // Registering without an origin removes the link. A restart cannot
    // erase either display invalidation from the native task's timeline.
    register(&host, "");
    assert!(visible(&host));
    wait_for("the exited attempt is reconciled after rejoin", || {
        job(&host, "managed")["metadata"]["stop_basis"] == "turn_exit"
    });
    let stopped = job(&host, "managed");
    assert_eq!(stopped["outcome_state"], "unknown");
    assert_eq!(stopped["status"], "failed");
    assert!(job(&host, "terminal")["terminal_at"].is_null());
    // A late completion for the wrong task is powerless. The receipted
    // task settles the inferred stop once, including after another restart.
    let finish = |task: &str| {
        json!({"type":"queue-operation","timestamp":stamp(),"content":format!(
        "<task-notification><tool-use-id>managed</tool-use-id><task-id>{task}</task-id><status>completed</status></task-notification>")})
    };
    append(&finish("foreign"));
    append(&finish("fixture-child"));
    wait_for(
        "the exact task receipt supersedes the inferred stop",
        || job(&host, "managed")["status"] == "succeeded",
    );
    let settled = host.get(&path);
    assert_eq!(settled["timeline"].as_array().unwrap().len(), 6);
    assert!(settled["job"]["metadata"].get("stop_basis").is_none());
    host.kill();
    let host = place.start_in(TOKEN, &flags, &env);
    let restored = host.get(&path);
    assert_eq!(restored["timeline"], settled["timeline"]);
    assert_eq!(restored["job"]["revision"], settled["job"]["revision"]);
    let challenge = |word: &str| {
        let sent = place.root.join("challenge");
        std::fs::write(&sent, word).unwrap();
        wait_for("the detached child answers a new challenge", || {
            std::fs::read_to_string(place.root.join("answer")).is_ok_and(|v| v == word)
        });
    };
    challenge("after the turn");
    host.kill();
    let host = place.start_in(TOKEN, &flags, &env);
    challenge("after another Host restart");
    std::fs::write(place.root.join("detached-end"), "").unwrap();

    // Exercise the checked-in launcher against this isolated Host's real
    // tools. The test controls when the harmless command may finish.
    let keeper = host.new_agent("Launcher");
    host.send(&keeper, r#"{"session":"script-fixture"}"#, "bind-script");
    wait_for("the launcher agent to be bound", || {
        host.state(&keeper) == "done"
    });
    let commitment=json!({"outcome":"Receive the command result","criteria":["The fixture runs"],"limits":"Fixture only","enroll":true}).to_string();
    let plan = goal(
        &host,
        &cwd,
        &[
            "create",
            &keeper,
            "work",
            "Read the result",
            "[]",
            &commitment,
        ],
    );
    let plan_id = plan["plan_id"].as_str().unwrap();
    let bin = place.root.join("commands");
    std::fs::create_dir(&bin).unwrap();
    for name in ["clarp-agent-bg", "clarp-goal"] {
        let cli = env!("CARGO_BIN_EXE_clarp-host");
        let stale = if name == "clarp-goal" {
            format!(
                "if [ \"$1\" = checkpoint ] && [ \"$FIXTURE_STALE_REVISION\" = yes ]; then exec '{cli}' tool clarp-goal checkpoint \"$2\" 999999 \"$4\"; fi\n"
            )
        } else {
            String::new()
        };
        program(
            &bin.join(name),
            &format!("#!/bin/sh\n{stale}exec '{cli}' tool {name} \"$@\"\n"),
        );
    }
    // No user service manager is needed for this local launch.
    program(&bin.join("systemctl"), "#!/bin/sh\nexit 1\n");
    let launcher = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../skills/clarp-background-jobs/scripts/run_detached_job.sh");
    let run = |id: &str, key: &str| {
        place.command(&launcher)
            .args(["--goal",id,&keeper,key,"fixture","Fixture command"])
            .arg(cwd.join(format!("{key}.log"))).args(["--","sh","-c",
                "printf 'started' > \"$1\"; while [ -d \"$3\" ] && [ ! -f \"$2\" ]; do sleep 0.05; done; printf 'done\\n'","fixture"])
            .arg(cwd.join(format!("{key}.started"))).arg(cwd.join("allow-end")).arg(&cwd)
            .env("PATH",format!("{}:/usr/bin:/bin",bin.display()))
            .env("FIXTURE_STALE_REVISION",if key=="refused-write"{"yes"}else{"no"}).env("CLARP_SESSION",&keeper).env("CLAUDE_PWA_SESSION",&keeper)
            .env("HOME",&place.root).env("CLARP_HOST_ADDR",&host.address)
            .env("CLARP_HOST_TOKEN",&host.token).current_dir(&cwd).ended()
    };
    let denied = run("nonexistent", "unstarted");
    assert_eq!(denied.status.code(), Some(4));
    assert!(!cwd.join("unstarted.started").exists());
    assert_eq!(host.ask("GET", "/background-jobs/unstarted", None).0, 404);
    // The real goal store refuses this stale checkpoint after the worker
    // registered. The launcher must not release the underlying command.
    let refused = run(plan_id, "refused-write");
    assert_eq!(
        refused.status.code(),
        Some(3),
        "{}",
        String::from_utf8_lossy(&refused.stderr)
    );
    wait_for("the refused launch closes its registered job", || {
        host.get("/background-jobs/refused-write")["job"]["status"] == "failed"
    });
    assert!(!cwd.join("refused-write.started").exists());
    let started = run(plan_id, "held-command");
    assert_eq!(
        started.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&started.stderr)
    );
    let handle = String::from_utf8(started.stdout).unwrap();
    assert_eq!(handle.trim(), "bg1:1:held-command");
    let registered = host.get("/background-jobs/held-command");
    assert_eq!(registered["job"]["status"], "running");
    assert!(
        registered["job"]["worker_pid"]
            .as_u64()
            .is_some_and(|pid| pid > 0)
    );
    let waiting = goal(&host, &cwd, &["get", plan_id]);
    assert_eq!(waiting["goal"]["continuation"]["job_handle"], handle.trim());
    assert_eq!(waiting["goal"]["continuation"]["state"], "waiting");
    std::fs::write(cwd.join("allow-end"), "").unwrap();
    wait_for("the goal wakes its agent on the command result", || {
        host.conversation(&keeper).iter().any(|(role, text)| {
            role == "user" && text.starts_with("Continue your durable outcome commitment.")
        })
    });
    let settled = goal(&host, &cwd, &["get", plan_id]);
    assert_eq!(
        settled["goal"]["continuation"]["dependency_result"]["outcome"],
        "succeeded"
    );
    assert_eq!(
        std::fs::read_to_string(cwd.join("held-command.log.exit"))
            .unwrap()
            .trim(),
        "0"
    );
}
