//! Speech fragments and authority of the side router in one live call.
use super::{offered, team};
use crate::python;
use serde_json::{Value,json};
use std::collections::VecDeque;

#[derive(Default)]
pub struct Routing{
    fragments: VecDeque<Fragment>,
    revision:u64,
    routed:u64,
    in_flight:bool,
    waiting:bool,
    calls:u8,
    last_user:f64,
    pub taken: Vec<Value>,
}

struct Fragment {role:&'static str, text:String, revision:u64}

pub struct Route {pub body:Value,pub waiting:bool,revision:u64}

impl Routing{
    pub fn observe(&mut self,event:&Value, now:f64){
        let role=match event["type"].as_str(){
            Some("session.delegation.created")=>{self.waiting=true;return;}
            Some("session.input_transcript.delta")=>"user",
            Some("session.output_transcript.delta")=>"assistant",
            _=>return,
        };
        let Some(delta)=event["delta"].as_str().filter(|s|!python::strip(s).is_empty()) else{return;};
        if role=="user"{self.revision+=1;self.last_user=now;}
        if self.fragments.back().is_none_or(|f|f.role!=role){
            self.fragments.push_back(Fragment{role,text:String::new(),revision:0});
            if self.fragments.len()>40{self.fragments.pop_front();}
        }
        if let Some(last)=self.fragments.back_mut(){
            last.text.push_str(delta);
            if let Some((i,_))=last.text.char_indices().rev().nth(3999){last.text.drain(..i);}
            if role=="user"{last.revision=self.revision;}
        }
    }

    pub fn begin(&mut self,snapshot:&Value, now:f64, local_time:&str)->Option<Route>{
        if self.in_flight || self.calls>=30 || (!self.waiting && (self.revision<=self.routed || now-self.last_user<1.2)){return None;}
        let cfg=&snapshot["live_tools"];
        let name=snapshot["profile"]["display_name"].as_str().filter(|v|!v.is_empty()).unwrap_or("the agent");
        let instructions=include_str!("routing.txt").replace("{name}",name).replace("{default_minutes}",&cfg["callback_minutes"]["default"].to_string()).replace("{team}",&team(cfg));
        let conversation:Vec<_>=self.fragments.iter().map(|f|json!({"role":f.role,"text":f.text})).collect();
        let current:Vec<_>=self.fragments.iter().filter(|f|f.role=="user" && f.revision>self.routed).map(|f|f.text.as_str()).collect();
        let request=json!({"conversation":conversation,"current_user_requests":current,"actions_taken":self.taken,"now":local_time});
        self.in_flight=true;self.calls+=1;
        Some(Route{revision:self.revision,waiting:self.waiting,body:json!({"model":"gpt-5.6-luna","input":python::dumps(&request),"instructions":instructions,"tools":offered(cfg),"parallel_tool_calls":true,"max_output_tokens":2000,"reasoning":{"effort":"low"}})})
    }

    pub fn finish(&mut self,route:Route,receipts:&[Value],failed:bool)->Vec<(&'static str,String)>{
        self.in_flight=false;self.waiting=false;
        self.routed=self.routed.max(if failed{self.revision}else{route.revision});
        if failed{return vec![("thinking","Host note: that turn could not be routed; nothing was handed off. Say so briefly if it matters.".into())];}
        if route.waiting && receipts.is_empty(){return vec![("commentary","Host note: no action was needed for that turn. Answer the user yourself.".into())];}
        super::receipts::notes(receipts).into_iter().map(|text|(if route.waiting{"commentary"}else{"thinking"},text)).collect()
    }
}
