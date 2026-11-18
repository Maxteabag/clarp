#![allow(clippy::unwrap_used)]
use clarp_kernel::rings::opening::{self as rules,Effects,Greeting,World};
use serde_json::{Value,json};

#[test]
fn every_claim_greeting_and_supersession_matches_the_reference(){
    let stories:Vec<Value>=serde_json::from_str(include_str!("fixtures/listen-first-vectors.json")).unwrap();
    let mut count=0;
    for story in stories{
        let mut op=story["initial"].clone();
        let mut generation=op["fence"]["host_gen"].clone();
        let mut row=Value::Null;
        for step in story["steps"].as_array().unwrap(){
            let cmd=&step["cmd"];let body=&step["body"];
            if !step["before"].is_null(){op=step["before"].clone();}
            if let Some(g)=cmd.get("gen"){generation=g.clone();}
            if let Some(q)=cmd.get("row"){
                row=if q.is_null(){Value::Null}else{json!({"status":q,"claimed_at":cmd["claimed_at"],"trace_id":cmd.get("trace").unwrap_or(&op["intent"]["trace_id"])})};
            }
            let at=step["at"].as_i64().unwrap();
            let owner=cmd["principal"].as_str().unwrap_or("device_one");
            let minted=step["written"]["owner_nonce"].as_str().unwrap_or("unused");
            let trace=step["written"]["intent"]["trace_id"].as_str().unwrap_or("unused");
            let w=World{now:at,host:"host-test",fence:None,queue:&row,generation:&generation,nonce:minted,trace};
            let mut effects=Effects::default();let before=op.clone();
            let action=cmd["do"].as_str().unwrap_or("claim");
            let (status,answer)=match action{
                "claim"=>rules::claim(&mut op,owner,body,&w,&mut effects),
                "greet"=>rules::greet(&mut op,Greeting{principal:owner,token:&body["token"],epoch:&body["epoch"],message:cmd["msg"].as_str().unwrap_or("hi"),trace:cmd["trace"].as_str().unwrap_or("trace-hi"),session:cmd["session"].as_str().unwrap_or("guard-session"),call:cmd["call_id"].as_str().unwrap_or("call-one")},&w,&mut effects),
                "retire"=>(200,json!(rules::retire(&mut op,cmd["reason"].as_str().unwrap_or("call_ended"),&w,&mut effects))),
                "admitted"=>{rules::greeting_admitted(&mut op,cmd["msg"].as_str().unwrap_or("hi"),cmd["ok"]==true,&w,&mut effects);(200,Value::Null)}
                _=>unreachable!(),
            };
            assert_eq!(json!(status),step["status"],"{} {cmd}",story["name"]);
            assert_eq!(answer,step["answer"],"{} {cmd}",story["name"]);
            if effects.dirty && action!="retire"{op["updated_at"]=json!(at);}
            let mut expected=before;
            for (k,v) in step["written"].as_object().unwrap(){expected[k]=v.clone();}
            assert_eq!(op,expected,"{} {cmd}",story["name"]);
            let changes=step["changes"].as_array().unwrap();
            let has=|kind:&str|changes.iter().any(|c|c[0]==kind);
            assert_eq!(effects.admit,has("admit"));
            assert_eq!(effects.withdraw,has("withdraw"));
            assert_eq!(effects.announce,has("announce"));
            if effects.withdraw {row["status"]=json!("withdrawn");}
            count+=1;
        }
    }
    assert_eq!(count,418);
}
