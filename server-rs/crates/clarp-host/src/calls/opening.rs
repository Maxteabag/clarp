//! Claims and greetings are serialized with changes to the owning call. Raw
//! tokens are accepted only in the request and never published on the stream.
use axum::{Extension,Json,body::Bytes,extract::State,http::StatusCode,response::{IntoResponse,Response}};
use clarp_kernel::{calls::{Asker, Refused},rings::opening as rules};
use serde_json::{Value,json};
use super::{Desk,With,caller};
use crate::{agent::Unavailable,edge::{App,Principal}};

pub(super) enum Change<'a>{
    Claim(&'a Value),
    Greeting {body:&'a Value,message:&'a str,trace:&'a str,session:&'a str,call:&'a str},
}

struct Applied {
    status:u16, answer:Value,
    admit:bool, withdraw:bool,
    state:Value,
}

impl Desk {
    async fn apply_opening(&self,app:&App,owner:&str,change:Change<'_>,queue:&Value)->Result<Applied,Unavailable>{
        self.rings(app,|rings,calls,with,(out,call_effects)|{
            let now=with.at.now_ms;
            let body=match &change{Change::Claim(b)|Change::Greeting{body:b,..}=>*b};
            let token=&body["token"];
            let found=rings.opening(token);
            let ring=found.map(|ring|ring.ring_id.clone()).unwrap_or_default();
            let fence=found.and_then(|ring|with.world.opening.and_then(|proof|rules::fence(ring,calls,with.at.host_id,proof)));
            let generation=proven(with);
            let nonce=format!("own_{}",crate::ids::hex(16));
            let trace=format!("callopen-{}",crate::ids::hex(6));
            let w=rules::World{generation:&generation,now,host:with.at.host_id,fence,queue,nonce:&nonce,trace:&trace};
            let perform=|op:&mut Value,fx:&mut rules::Effects|match &change{
                Change::Claim(body)=>rules::claim(op,owner,body,&w,fx),
                Change::Greeting{body,message,trace,session,call}=>rules::greet(op,rules::Greeting{
                    principal:owner,token:&body["token"],epoch:&body["epoch"],message,trace,session,call},&w,fx),
            };
            if let Change::Greeting{call,session,..}=&change && let Err(failed)=calls.exact_recipient(owner,call,session){
                return Applied{status:failed.status,answer:failed.body,admit:false,withdraw:false,state:Value::Null};
            }
            let ((status,answer,state),fx)=rings.change_opening(&ring,now,out,|ring,fx|{
                let (status,answer)=perform(&mut ring.opening,fx);
                (status,answer,ring.opening.clone())
            }).unwrap_or_else(||{
                let mut fx=rules::Effects::default(); let (status,answer)=perform(&mut Value::Null,&mut fx);
                ((status,answer,Value::Null),fx)
            });
            if fx.announce {
                let claim=body["claim"].as_str().unwrap_or("greeting");
                rings.opening_audit(&ring,claim,owner,answer["route"].as_str(),now,out);
            }
            if status==200 && let Change::Greeting{call,session,..}=change{
                let asker=Asker{caller:owner,call_id:call,..Default::default()};
                if let Err(Refused{status,body})=calls.route(asker,session,with.at,call_effects){
                    return Applied{status,answer:body,admit:false,withdraw:false,state};
                }
            }
            Applied{status,answer,admit:fx.admit,withdraw:fx.withdraw,state}
        }).await
    }

    pub(crate) async fn greeting(&self,app:&App,owner:&str,change:Change<'_>)->Result<Result<Value,Refused>,Unavailable>{
        let applied=self.apply_opening(app,owner,change,&Value::Null).await?;
        if applied.status!=200{return Ok(Err(Refused{status:applied.status,body:applied.answer}));}
        Ok(Ok(applied.answer))
    }

    pub(crate) async fn greeting_admitted(&self,app:&App,id:&str,message:&str,ok:bool)->Result<(),Unavailable>{
        self.rings(app,|rings,_,with,(out,_)|{
            let g=proven(with);
            let world=rules::World{generation:&g,now:with.at.now_ms,host:with.at.host_id,fence:None,queue:&Value::Null,nonce:"",trace:""};
            rings.change_opening(id,world.now,out,|ring,fx|rules::greeting_admitted(&mut ring.opening,message,ok,&world,fx));
        }).await
    }
}

fn proven(with:&With)->Value{
    let Some(proof)=with.world.opening else{return Value::Null;};
    let first=proof.generation();if first==proof.generation(){first}else{Value::Null}
}

pub(super) async fn claim(State(app):State<App>,Extension(principal):Extension<Principal>,body:Bytes)->Response{
    let owner=match caller(&principal){Ok(who)=>who,Err(err)=>return err.into_response()};
    let body:Value=serde_json::from_slice(&body).unwrap_or(Value::Null);
    match app.calls.apply_opening(&app,&owner,Change::Claim(&body),&Value::Null).await {
        Ok(applied)=>{
            let status=StatusCode::from_u16(applied.status).unwrap_or(StatusCode::CONFLICT);
            (status,Json(applied.answer)).into_response()
        }
        Err(failed)=>crate::edge::ApiError::from(failed).into_response(),
    }
}
