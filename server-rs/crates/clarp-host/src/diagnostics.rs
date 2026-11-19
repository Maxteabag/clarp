//! Capture is local to this Host. Producers never acquire a file or wait for space.
use axum::{Json,body::Bytes,extract::{State,Request,MatchedPath},http::StatusCode,middleware::Next,response::Response};
use clarp_kernel::diagnostics::{self as rules,Settings};
use clarp_ports::{clock::Clock,diagnostics::DiagnosticStore};
use serde_json::{json,Value};
use std::{collections::BTreeMap,sync::{Arc,Mutex,atomic::{AtomicU16,AtomicU64,Ordering}},path::{Path,PathBuf},time::{Duration,Instant}};
use tokio::sync::{mpsc,watch};
use crate::edge::{App,ApiError};
pub const CAP:u64=256*1024*1024;
const MAX_RECORD:usize=32*1024;
type Row=(i64,Vec<u8>);
pub struct Capture{
    bits:AtomicU16,
    queue:mpsc::Sender<Row>,
    dropped:Arc<AtomicU64>,
    settings_lock:tokio::sync::Mutex<()>,
    resources:watch::Sender<bool>,
    health:Mutex<BTreeMap<&'static str,Value>>,
}
impl Capture{
    pub fn start(mut store:Box<dyn DiagnosticStore>,clock:Arc<dyn Clock+Send+Sync>,setting:Settings,data:PathBuf)->Arc<Self>{
        let (queue,mut rx)=mpsc::channel::<Row>(256);
        let (resources,mut mode)=watch::channel(setting.allows("resources"));
        let dropped=Arc::new(AtomicU64::new(0));
        let desk=Arc::new(Self{bits:AtomicU16::new(setting.0),queue,dropped:dropped.clone(),settings_lock:Default::default(),resources,health:Default::default()});
        let timer=clock.clone();
        tokio::spawn(async move{
            loop{
                let now=timer.now_ms();
                let delay=3_600_000-now.rem_euclid(3_600_000);
                let mut batch=Vec::new();
                if tokio::time::timeout(Duration::from_millis(delay as u64),rx.recv_many(&mut batch,64)).await .is_ok_and(|n|n==0){break;}
                let now=timer.now_ms();
                let Ok((returned,lost))=tokio::task::spawn_blocking(move||{
                    let lost=store.append(now,&batch).unwrap_or(batch.len());(store,lost)
                }).await else{break;};
                store=returned;dropped.fetch_add(lost as u64,Ordering::Relaxed);
            }
        });
        let weak=Arc::downgrade(&desk);
        tokio::spawn(async move{
            loop{
                if mode.wait_for(|on|*on).await.is_err(){break;}
                tokio::select!{
                    change=mode.changed()=>{if change.is_err(){break;}},
                    ()=tokio::time::sleep(Duration::from_secs(15))=>{
                        let Some(desk)=weak.upgrade() else{break;};
                        if !desk.settings().allows("resources"){continue;}
                        let dir=data.clone();
                        let at=clock.now_ms();
                        let _=tokio::task::spawn_blocking(move||desk.emit("resources",at,||sample(&dir))).await;
                    }
                }
            }
        });
        desk
    }
    fn settings(&self)->Settings{Settings(self.bits.load(Ordering::Acquire))}
    pub fn emit(&self,category:&'static str,now:i64,make:impl FnOnce()->Value)->bool{
        let s=self.settings();
        if !(if category=="requests"{s.requests()}else{s.allows(category)}){return false;}
        let Ok(slot)=self.queue.try_reserve() else{self.dropped.fetch_add(1,Ordering::Relaxed);return false;};
        let value=json!({"category":category,"at":now,"event":make()});
        let bytes=value.to_string().into_bytes();
        if bytes.len()>MAX_RECORD{self.dropped.fetch_add(1,Ordering::Relaxed);return false;}
        slot.send((now,bytes));true
    }
    pub fn mark(&self,subsystem:&'static str,ok:bool,at:i64){
        let mut entries=self.health.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
        let e=entries.entry(subsystem).or_insert_with(||json!({"last_success_at":null,"last_error_at":null,"last_error":null}));
        let time=json!(at as f64/1000.0);
        if ok{e["last_success_at"]=time;}else{e["last_error_at"]=time;e["last_error"]=json!("failed");}
    }
}
fn sample(dir:&Path)->Value{
    let mut v=json!({"kind":"sample"});
    if let Ok(s)=std::fs::read_to_string("/proc/self/status"){
        for (prefix,key) in [("VmHWM:","process_rss_kb"),("Threads:","process_threads")]{
            if let Some(n)=s.lines().find_map(|l|l.strip_prefix(prefix)).and_then(|s|s.split_whitespace().next()).and_then(|s|s.parse::<u64>().ok()){v[key]=json!(n);}
        }
    }
    if let Some(n)=std::fs::read_to_string("/proc/loadavg").ok().and_then(|s|s.split_whitespace().next()?.parse::<f64>().ok()){v["load_1m"]=json!(n);}
    for kind in ["cpu","memory","io"]{
        if let Ok(s)=std::fs::read_to_string(format!("/proc/pressure/{kind}")){
            for field in s.lines().next().unwrap_or_default().split_whitespace(){
                if let Some((k,n))=field.split_once('=') && matches!(k,"avg10"|"avg60") && let Ok(n)=n.parse::<f64>(){v[format!("{kind}_{k}")]=json!(n);}
            }
        }
    }
    v["state_bytes"]=json!(std::fs::metadata(dir.join("fleet.db")).map_or(0,|m|m.len()));
    use clarp_ports::diagnostics::DiagnosticUsage;
    let usage=clarp_store_files::diagnostics::HourFiles::new(dir.join("diagnostics"),CAP).usage();
    if let Ok((_,bytes))=usage{v["telemetry_bytes"]=json!(bytes);}
    v
}
pub async fn settings(State(app):State<App>)->Json<Value>{
    Json(json!({"ok":true,"settings":app.capture.settings().public()}))
}
pub async fn update(State(app):State<App>,body:Bytes)->Result<Json<Value>,ApiError>{
    let bad=|msg:String|ApiError::new(StatusCode::BAD_REQUEST,msg);
    let v:Value=if body.is_empty(){json!({})}else{serde_json::from_slice(&body).map_err(|_|bad("bad json".into()))?};
    if !v.is_object(){return Err(bad("bad json".into()));}
    let s=Settings::update(&v).map_err(bad)?;
    let _lock=app.capture.settings_lock.lock().await;
    app.fleet.set(vec![(rules::KEY.into(),s.kept().to_string())]).await?;
    app.capture.bits.store(s.0,Ordering::Release);
    app.capture.resources.send_if_modified(|old|{let new=s.allows("resources");let changed=*old!=new;*old=new;changed});
    Ok(Json(json!({"ok":true,"settings":s.public()})))
}
pub async fn health(State(app):State<App>)->Json<Value>{
    let subsystems=json!(*app.capture.health.lock().unwrap_or_else(std::sync::PoisonError::into_inner));
    let (pending,active)=app.mouth.queue_counts();
    let mut reply=rules::health(app.ears.ready(),true,subsystems,pending,active,&app.config.deployed_version);
    let dropped=app.capture.dropped.load(Ordering::Relaxed);
    if dropped>0{reply["diagnostic_events_dropped"]=json!(dropped);}
    Json(reply)
}
pub async fn request(State(app):State<App>,req:Request,next:Next)->Response{
    if !app.capture.settings().requests(){return next.run(req).await;}
    let pattern=req.extensions().get::<MatchedPath>().map(|p|p.as_str().to_owned()).unwrap_or_else(||"unmatched".into());
    let method=match req.method().as_str(){m @ ("GET"|"POST"|"DELETE"|"HEAD"|"PUT"|"PATCH"|"OPTIONS")=>m,_=>"other"}.to_owned();
    let start=Instant::now();let reply=next.run(req).await;
    app.capture.emit("requests",app.clock.now_ms(),||json!({"method":method,"route":pattern,"status":reply.status().as_u16(),"duration_ms":start.elapsed().as_millis()}));reply
}
pub async fn clog(State(app):State<App>,body:Bytes)->Result<Json<Value>,ApiError>{
    let s=app.capture.settings();
    if !s.uploads(){
        // Validate the JSON without constructing any strings or event rows.
        let mut de=serde_json::Deserializer::from_slice(&body);
        if body.iter().copied().find(|b|!b.is_ascii_whitespace())!=Some(b'{') || <serde::de::IgnoredAny as serde::Deserialize>::deserialize(&mut de).is_err() || de.end().is_err(){return Err(ApiError::new(StatusCode::BAD_REQUEST,"bad json"));}
        return Ok(Json(json!({"ok":true,"captured":false})));
    }
    let Ok(v @ Value::Object(_))=serde_json::from_slice::<Value>(&body) else{return Err(ApiError::new(StatusCode::BAD_REQUEST,"bad json"));};
    let items=v["events"].as_array().map_or_else(||std::slice::from_ref(&v),Vec::as_slice);
    let mut captured=false;
    for item in items{
        let name=item["event"].as_str().unwrap_or("log");
        let category=rules::client_category(name);
        captured|=app.capture.emit(category,app.clock.now_ms(),||{
            let detail=&item["detail"];
            let detail=if detail.to_string().len()>MAX_RECORD{json!({"truncated":true})}else{rules::safe_detail(detail)};
            json!({"kind":"client_upload","event_id":rules::masked(&json!(name)),"detail":detail})
        });
    }
    Ok(Json(if captured{json!({"ok":true})}else{json!({"ok":true,"captured":false})}))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests{
    use super::*;
    #[test]
    fn diagnostic_queue_drops_new_events_without_building_them(){
        let (queue,mut read)=mpsc::channel(1);
        let capture=Capture{bits:AtomicU16::new(0),queue,dropped:Default::default(),settings_lock:Default::default(),resources:watch::channel(false).0,health:Default::default()};
        assert!(!capture.emit("agents",0,||panic!("allocated while disabled")));
        capture.bits.store(Settings::update(&json!({"enabled":true,"categories":["agents"]})).unwrap().0,Ordering::Release);
        assert!(capture.emit("agents",0,||json!({"kind":"first"})));
        for _ in 0..128{assert!(!capture.emit("agents",1,||panic!("allocated while full")));}
        assert_eq!(capture.dropped.load(Ordering::Relaxed),128);
        let (_,first)=read.try_recv().unwrap();assert_eq!(serde_json::from_slice::<Value>(&first).unwrap()["event"]["kind"],"first");
        assert!(!capture.emit("agents",2,||Value::String("x".repeat(MAX_RECORD))));
        assert!(read.try_recv().is_err());
    }
}
