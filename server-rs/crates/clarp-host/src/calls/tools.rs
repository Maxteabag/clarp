//! Effects for the frozen tool team of one joined voice call. The side router
//! never blocks the voice socket. Only its session may append the returned note.
use std::sync::{Arc,atomic::{AtomicBool,Ordering}};
use std::time::Duration;
use clarp_kernel::{live_call::{self,callback,receipts,routing::Route,team::{self,Action,Prompt,Team}},rings::Joined};
use serde_json::{Value,json};
use crate::edge::App;

pub struct Worker {
    pub app:App,
    pub call:Joined,
    pub active:Arc<AtomicBool>,
    pub team:Team,
    pub taken:Vec<Value>,
}

pub fn time(epoch_ms:i64)->String{
    let zone=clarp_kernel::oracle::threads::zone_named(&crate::oracle::zone_of_this_machine());
    clarp_kernel::oracle::threads::iso_time(epoch_ms,&zone).and_then(|iso|iso.get(11..16).map(str::to_owned)).unwrap_or_else(||"?".into())
}

impl Worker {
    pub async fn current(&self)->bool{
        self.active.load(Ordering::Acquire) && self.app.fleet.directory().can_receive(&self.call.principal)
            && self.app.calls.step(&self.app,(&self.call.call_id,self.call.call_epoch),|book|book.holds()).await.unwrap_or(false)
    }

    fn binding(&self)->callback::Binding<'_>{
        callback::Binding{host:&self.app.server_id,principal:&self.call.principal,
            agent:self.call.snapshot["agent"]["session"].as_str().unwrap_or(""),call:&self.call.call_id}
    }

    async fn save(&self,key:&str,row:&Value)->bool{
        self.app.fleet.keep("live_tools",key.into(),Some(row.to_string().into_bytes())).await.is_ok()
    }

    async fn callback(&self,name:&str,args:&Value,explicit:bool)->Value{
        let b=self.binding();
        let slot=b.slot();
        let known=self.app.fleet.kept("live_tools",slot.clone()).await;
        let mut row=match known {
            Ok(Some(bytes))=>match serde_json::from_slice(&bytes){Ok(value)=>value,Err(_)=>return failed(name)},
            Ok(None)=>Value::Null,
            Err(_)=>return failed(name),
        };
        let principal=self.call.principal.clone();
        let eligible=self.app.calls.rings(&self.app,|rings,_,with,_|rings.callback_recipient(&principal,&with.world)).await.unwrap_or(false);
        let r=callback::Request{config:&self.call.snapshot["live_tools"],tool:name,args,explicit,eligible};
        let result=tokio::task::block_in_place(||{
            let mut effects=Callback{worker:self,slot:&slot,rt:tokio::runtime::Handle::current()};
            callback::execute(&b,r,&mut row,&mut effects)
        });
        match result {Ok(receipt)=>receipt,Err(callback::Failure::Ended)=>team::ended(name),Err(callback::Failure::Ledger)=>failed(name)}
    }

    pub async fn execute(&mut self,tool:&str,args:&Value,explicit:bool)->Value{
        if !self.current().await{return team::ended(tool);}
        if matches!(tool,"schedule_callback"|"cancel_callback"){return self.callback(tool,args,explicit).await;}
        let cfg=&self.call.snapshot["live_tools"];
        let members=cfg["agents"].as_array().map_or(&[][..],Vec::as_slice);
        let proposed=self.team.propose(members,tool,args);
        match proposed.action{
            Action::Answer(answer)=>answer,
            Action::Status|Action::Transcript(_)=>{
                let limit=if let Action::Transcript(n)=proposed.action{n}else{3};
                let data=crate::oracle::live_read(&self.app,&self.call.principal,tool,proposed.session,limit).await;
                team::read(tool,proposed.name,data)
            }
            Action::Delegate{request,normalized}=>{
                let b=self.binding();
                let nonce=crate::ids::hex(16);
                let id=team::operation("rq-",&[b.host,b.call,proposed.session,&nonce]);
                let mut row=json!({"kind":"delegate_request","status":"claimed","status_text":"requesting","at":self.app.clock.now_ms() as f64/1000.0,"to":proposed.session,"host_id":b.host,"principal":b.principal,"agent_session":b.agent,"call_id":b.call});
                if !self.save(&id,&row).await{return failed(tool);}
                if !self.current().await{return team::ended(tool);}
                let receipt=if cfg["delegate"]["kind"]=="prompt" {
                    let name=self.call.snapshot["profile"]["display_name"].as_str().filter(|s|!s.is_empty()).unwrap_or("the agent");
                    let args=["--to".into(),proposed.session.into(),"--from".into(),b.agent.into(),"--text".into(),team::prompt(name,&normalized)];
                    let result=crate::admin_tool::prompt(&self.app,&args).await;
                    let verdict=match result.exit{0=>Prompt::Sent,4=>Prompt::Refused(&result.stderr),_=>Prompt::Uncertain(&result.stderr)};
                    team::prompted(proposed.name,&id,verdict)
                }else{
                    let body=json!({"call_id":b.call,"principal":b.principal,"from":b.agent,"agent":proposed.session,"request":request});
                    let answer=tokio::task::block_in_place(||post(&cfg["delegate"]["url"],&body,30));
                    let outcome=match answer {
                        Some((code,answer))=>receipts::director(&answer,Some(i64::from(code))),
                        None=>json!({"state":"unconfirmed","status":"unknown_delivery","note":"regi's answer was unclear; not retried"}),
                    };
                    let mut receipt=json!({"tool":tool,"agent":proposed.name,"request_id":id});
                    if let Some(fields)=outcome.as_object(){
                        for (k,v) in fields {if k!="state" && !v.is_null(){receipt[k]=v.clone();}}
                    }
                    receipt
                };
                row["status_text"]=receipt["status"].clone();
                row["status"]=json!(match receipt["status"].as_str(){Some("refused"|"not_forwarded")=>"not_done",Some("unknown_delivery")=>"unconfirmed",_=>"done"});
                row["receipt"]=receipt.clone();
                let _=self.save(&id,&row).await;
                receipt
            }
        }
    }

    pub async fn route(&mut self,key:&str,route:&Route)->Option<Vec<Value>>{
        if !self.current().await{return None;}
        let response=crate::oracle::route_voice(&self.app,key,route.body.clone()).await?;
        let mut receipts=Vec::new();
        for item in response["output"].as_array().into_iter().flatten().filter(|it|it["type"]=="function_call").take(8){
            let args=item["arguments"].as_str().and_then(|s|serde_json::from_str::<Value>(s).ok()).filter(Value::is_object).unwrap_or_else(||json!({}));
            let receipt=self.execute(item["name"].as_str().unwrap_or(""),&args,true).await;
            if receipt["status"]=="call_ended"{receipts.push(receipt);break;}
            if matches!(receipt["status"].as_str(),Some("forwarded"|"queued_at_regi"|"already_present"|"unknown_delivery"|"scheduled"|"already_scheduled"|"cancelled"|"nothing_pending")){
                self.taken.push(receipt.clone());
            }
            receipts.push(receipt);
        }
        Some(receipts)
    }
}

fn failed(tool:&str)->Value{json!({"tool":tool,"status":"failed","error":"ledger unavailable"})}

fn post(url:&Value,body:&Value,seconds:u64)->Option<(u16,Value)>{
    let url=live_call::on_this_machine(Some(url))?;
    let agent:ureq::Agent=ureq::Agent::config_builder().proxy(None).max_redirects(0).timeout_global(Some(Duration::from_secs(seconds))).http_status_as_error(false).build().into();
    let mut result=agent.post(url).send_json(body).ok()?;
    let code=result.status().as_u16();
    if matches!(code,300..=399|404|405){return None;}
    let data=result.body_mut().with_config().limit(65536).read_to_vec().ok()?;
    let reply=if data.is_empty(){json!({})}else{serde_json::from_slice(&data).ok()?};
    if !reply.is_object(){return None;}
    Some((if (200..300).contains(&code){200}else{code},reply))
}

struct Callback<'a>{worker:&'a Worker,slot:&'a str,rt:tokio::runtime::Handle}
impl callback::Director for Callback<'_>{
    fn post(&mut self,body:&Value)->Result<Option<(u16,Value)>,callback::Failure>{
        if !self.rt.block_on(self.worker.current()){return Err(callback::Failure::Ended);}
        Ok(post(&self.worker.call.snapshot["live_tools"]["callback"]["url"],body,10))
    }
    fn save(&mut self,row:&Value)->Result<(),callback::Failure>{
        self.rt.block_on(self.worker.save(self.slot,row)).then_some(()).ok_or(callback::Failure::Ledger)
    }
    fn time(&self,epoch:&Value)->String{
        epoch.as_f64().map_or_else(||"?".into(),|s|time((s*1000.0) as i64))
    }
}
