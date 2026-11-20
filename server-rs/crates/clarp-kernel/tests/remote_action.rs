#![allow(clippy::unwrap_used)]
use clarp_kernel::{devices,remote_action};
use serde_json::{Value,json};

#[test]
fn shortcut_events_and_refusals_match_the_reference() {
    let fixture:Value=serde_json::from_str(include_str!("fixtures/remote-action.json")).unwrap();
    let now=fixture["now"].as_i64().unwrap();
    let mut count=0;
    for row in fixture["rows"].as_array().unwrap(){
        let method=row[0].as_str().unwrap();
        let input=row[1].as_str().unwrap();
        let result=if method=="GET" {remote_action::get(input,now)}else{
            remote_action::post(input.as_bytes(),now,||fixture["draw"].as_str().unwrap().to_owned())
        };
        let (status,body,events)=match result{
            Err(why)=>(400,json!({"error":why}),json!([])),
            Ok(event)=>{
                let reply=if method=="GET"{json!(remote_action::PAGE)}else{remote_action::receipt(&event)};
                (200,reply,json!([event]))
            }
        };
        assert_eq!((json!(status),body,events),(row[2].clone(),row[3].clone(),row[4].clone()),"{method} {input}");
        count+=1;
    }
    for method in ["POST","GET"]{
        assert_eq!(json!(devices::limited_refused(method,"/remote-action")),fixture["limited"][method]);
    }
    assert_eq!(count,150);
}
