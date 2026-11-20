use super::*;

#[test]
fn a_move_names_every_render_spec_left_on_the_source_and_takes_its_other_artifacts() {
    let place = Place::new("import-render-specs");
    let host = place.start();
    let sessions = ["sample", "single", "plain", "broken"];
    let roster: Vec<_> = sessions.iter().map(|session| json!({
        "agent_id":session, "session":session, "persona":session,
        "backend":"scripted","cwd":place.root.join("work")
    })).collect();
    let item = |session:&str, kind:&str, id:&str, title:&str| json!({
        "agent_id":session,"session":session,"artifact_id":id,"type":kind,"status":"ready",
        "title":title,"created_at":1000,"updated_at":2000
    });
    let mut page = Vec::new();
    for i in 0..499 {
        page.push(item("sample","render_spec",&format!("spec-{i}"),"Same title"));
    }
    page[0]["summary"]=json!("See /media/asset_only_spec");
    page.push(item("sample","document","doc-first","Before the boundary"));
    let mut second_doc=item("sample","data","data-last","After the boundary");
    second_doc["summary"]=json!("See /media/asset_shared");
    let mut second_spec=item("sample","render_spec","spec-final","Two\nlines");
    second_spec["summary"]=json!("See /media/asset_shared");
    let list=|v:Vec<Value>|json!({"artifacts":v}).to_string();
    let (source,requests)=answers(vec![
        ("/agents/snapshot","200 OK",json!({"agents":roster,"personas":[]}).to_string()),
        ("/log","200 OK",json!({"turns":[{"id":"row-1","role":"assistant","text":"Moved history"}]}).to_string()),
        ("/task-plans","200 OK",json!({"plans":[]}).to_string()),
        ("/artifacts?session=sample&order=created&limit=500&offset=0","200 OK",list(page)),
        ("/artifacts?session=sample&order=created&limit=500&offset=500","200 OK",list(vec![second_spec,second_doc])),
        ("/artifacts?session=single","200 OK",list(vec![item("single","render_spec","spec-only","Only item")])),
        ("/artifacts?session=plain","200 OK",list(vec![item("plain","document","doc-plain","Plain")])),
        ("/artifacts?session=broken","200 OK",list(vec![item("broken","document","doc-before-failure","Valid"),item("broken","unknown","unsupported","Unknown")])),
        ("/media?","200 OK",json!({"assets":[]}).to_string()),
        ("/media/asset_shared","200 OK","shared content".into()),
        ("/agent-portraits","200 OK",json!({"portraits":[]}).to_string()),
        ("/transcription-providers","200 OK","{}".into()),
    ]);
    let import=|session:&str,extra:&[&str]|{
        Command::new(env!("CARGO_BIN_EXE_clarp-host"))
            .args(["import","--from",&source,"--host",&host.address,"--session",session])
            .args(extra).env_remove("CLARP_SOURCE_TOKEN").env_remove("CLARP_HOST_TOKEN").ended()
    };
    let report=|output:std::process::Output|{
        assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
        String::from_utf8(output.stdout).unwrap()
    };
    let moved=report(import("sample",&[]));
    assert!(moved.contains("the 2 things in its library"));
    assert_eq!(host.conversation("sample"),[said("assistant","Moved history")]);
    let listed=host.get("/artifacts?session=sample");
    let kept=listed["artifacts"].as_array().unwrap();
    assert_eq!(kept.len(),2);
    for (id,title) in [("doc-first","Before the boundary"),("data-last","After the boundary")]{
        assert!(kept.iter().any(|row|row["artifact_id"]==id && row["title"]==title));
    }
    for id in ["spec-0","spec-498","spec-final"] {
        assert_eq!(host.ask("GET",&format!("/artifacts/{id}"),None).0,404);
    }
    for text in [moved,report(import("sample",&["--files"]))] {
        let mut lines=text.lines();
        assert!(lines.any(|l|l=="Left on the source Host: 500 render specs."));
        for i in 0..499 {assert_eq!(lines.next().unwrap(),format!("  \"spec-{i}\" — \"Same title\""));}
        assert_eq!(lines.next(),Some("  \"spec-final\" — \"Two\\nlines\""));
    }
    {
        let asked=requests.lock().unwrap();
        assert_eq!(asked.iter().filter(|r|r.starts_with("GET /media/asset_shared ")).count(),2);
        assert!(!asked.iter().any(|r|r.starts_with("GET /media/asset_only_spec ")));
    }
    let one=report(import("single",&[]));
    assert!(one.contains("Left on the source Host: 1 render spec.\n  \"spec-only\" — \"Only item\"\n"));
    assert!(host.get("/artifacts?session=single")["artifacts"].as_array().unwrap().is_empty());
    assert!(!report(import("plain",&[])).contains("Left on the source Host"));
    let refused=import("broken",&[]);
    assert!(!refused.status.success());
    assert!(String::from_utf8_lossy(&refused.stderr).contains("unsupported artifact type"));
    assert_eq!(host.ask("GET","/artifacts/doc-before-failure",None).0,404);
}
