#![allow(clippy::unwrap_used)]
use clarp_kernel::vocabulary::{self, Budget, Pack};
use serde_json::{Value, json};

#[test]
fn budgets_and_compilations_match_the_python_reference() {
    let v: Value =
        serde_json::from_str(include_str!("fixtures/vocabulary-packs-vectors.json")).unwrap();
    assert_eq!(json!(*vocabulary::BUDGETS), v["declared"]);
    for case in v["measured"].as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        assert_eq!(case["tokens"], vocabulary::estimate_tokens(text));
        assert_eq!(case["rarity"], vocabulary::rarity(text));
    }
    for case in v["budgets"].as_array().unwrap() {
        assert_eq!(
            case["result"],
            json!(vocabulary::budget_for(
                case["provider"].as_str().unwrap(),
                case["model"].as_str().unwrap()
            ))
        );
    }
    for case in v["stories"].as_array().unwrap() {
        let budget: Budget = serde_json::from_value(case["budget"].clone()).unwrap();
        let packs: Vec<Pack> = serde_json::from_value(case["packs"].clone()).unwrap();
        assert_eq!(
            case["audit"],
            vocabulary::compile(&packs, &budget),
            "{case}"
        );
    }
    for case in v["sources"].as_array().unwrap() {
        let sources: vocabulary::sources::Sources =
            serde_json::from_value(case["input"].clone()).unwrap();
        assert_eq!(json!(sources.packs()), case["packs"]);
    }
    for case in v["workspaces"].as_array().unwrap() {
        let paths: Vec<String> = serde_json::from_value(case["paths"].clone()).unwrap();
        assert_eq!(json!(vocabulary::sources::identifiers(&paths)), case["ids"]);
    }
    for case in v["verdicts"].as_array().unwrap() {
        let text = case["transcript"].as_str().unwrap();
        assert_eq!(
            case["discard"],
            vocabulary::judged_text(text, case["probability"].as_f64()).is_empty()
        );
    }
}

#[test]
fn the_book_matches_each_reference_admin_request() {
    use vocabulary::book::Book;
    let v: Value=serde_json::from_str(include_str!("fixtures/vocabulary-packs-vectors.json")).unwrap();
    let mut book=Book::default();
    let mut count=0;
    for step in v["admin"].as_array().unwrap(){
        let route=step["route"].as_str().unwrap();
        let result=if step["method"]=="GET" {book.read(route,&step["data"])}
        else {book.clone().change(route,&step["data"],1000,"example",false).map(|(next,response)|{book=next;response})};
        let (status,value)=match result{Ok(value)=>(200,value),Err((status,why))=>(status,json!({"error":why}))};
        assert_eq!(json!([status,value]),step["response"],"{step}");
        count+=1;
    }
    assert_eq!(count,43);
}

#[test]
fn names_members_and_assignments_survive_round_trips_and_delete_with_their_owners(){
    use vocabulary::book::Book;
    let mut b=Book::default();
    let apply=|b:&mut Book,action:&str,v:Value,suffix:&str| {
        let (next,out)=b.clone().change(action,&v,20,suffix,true).unwrap();
        *b=next;out
    };
    for (id,name) in [("a","One"),("b","Two")]{
        apply(&mut b,"packs",json!({"name":name,"terms":["First","Second"]}),id);
        apply(&mut b,"profiles",json!({"name":name,"pack_ids":["pk-a"]}),id);
    }
    for (op,body) in [
        ("packs",json!({"name":"One"})),
        ("profiles",json!({"name":"Two"})),
        ("packs/update",json!({"pack_id":"pk-a","name":"Two","enabled":false})),
        ("terms/update",json!({"term_id":1,"text":"sEConD","rarity":1})),
    ]{assert!(matches!(b.clone().change(op,&body,21,"c",true),Err((409,_))));}
    let both=apply(&mut b,"terms",json!({"pack_id":"pk-b","text":"first"}),"");
    assert_eq!(both["added"],false);
    apply(&mut b,"profiles/packs",json!({"profile_id":"pf-a","pack_id":"pk-b","position":-1}),"");
    for (k,id) in [("agent_id","alpha"),("team_id","team")]{
        apply(&mut b,"assign",json!({"profile_id":"pf-a",k:id}),"");
        apply(&mut b,"assign",json!({"profile_id":"pf-b",k:id}),"");
    }
    let detail=|b:&Book,id:&str|b.read("profile",&json!({"profile_id":id})).unwrap();
    assert_eq!(detail(&b,"pf-a")["assignments"],json!([]));
    assert_eq!(detail(&b,"pf-b")["assignments"].as_array().unwrap().len(),2);
    assert_eq!(detail(&b,"pf-a")["packs"][0]["pack_id"],"pk-b");
    let restored:Book=serde_json::from_slice(&serde_json::to_vec(&b).unwrap()).unwrap();
    assert_eq!(detail(&restored,"pf-b"),detail(&b,"pf-b"));
    let (_,_,packs)=restored.assigned("alpha").unwrap();
    assert_eq!(packs.len(),1);assert_eq!(packs[0].terms.len(),2);
    assert_eq!(packs[0].name,"One");assert!(packs[0].enabled);
    assert!(b.clone().change("assign",&json!({"profile_id":"pf-missing","agent_id":"alpha"}),21,"",true).is_err());
    apply(&mut b,"packs/delete",json!({"pack_id":"pk-a"}),"");
    assert_eq!(detail(&b,"pf-b")["packs"],json!([]));
    assert_eq!(b.read("terms",&json!({"pack_id":"pk-a"})).unwrap()["terms"],json!([]));
    assert_eq!(detail(&b,"pf-a")["packs"].as_array().unwrap().len(),1);
    apply(&mut b,"profiles/delete",json!({"profile_id":"pf-b"}),"");
    assert!(b.assigned("alpha").is_none());
    assert_eq!(b.read("assignments",&Value::Null).unwrap()["assignments"],json!([]));
}
