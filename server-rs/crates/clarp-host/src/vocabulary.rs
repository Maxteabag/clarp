//! The vocabulary managed through the app. Database work runs outside the
//! request runtime; each change commits its pack, terms and assignments together.
use std::collections::HashMap;
use axum::{Json,Router,body::Bytes,extract::{OriginalUri,Query,State},http::StatusCode,routing::{get,post}};
use serde_json::{Value,json};
use crate::edge::{App,ApiError};
mod store;

async fn perform(app:&App,action:String,data:Value,write:bool)->Result<Json<Value>,ApiError>{
    let root=app.config.data_dir.clone();
    let now=app.clock.now_ms();
    let directory=app.fleet.directory();
    let agent=data["agent_id"].as_str().unwrap_or("").trim();
    let known=directory.members.iter().any(|m|m.record.agent_id==agent);
    let result=tokio::task::spawn_blocking(move||{
        let mut db=store::open(&root)?;
        let mode=if write{rusqlite::TransactionBehavior::Immediate}else{rusqlite::TransactionBehavior::Deferred};
        let tx=db.transaction_with_behavior(mode).map_err(store::unavailable)?;
        let value=if write{store::change(&tx,&action,&data,now,known)?} else {store::read(&tx,&action,&data)?};
        tx.commit().map_err(store::unavailable)?;
        Ok::<_,store::Failure>(value)
    }).await.map_err(|_|ApiError::new(StatusCode::SERVICE_UNAVAILABLE,"vocabulary worker ended"))?;
    result.map(Json).map_err(|(status,why)|ApiError::new(status,why))
}

async fn view(State(app):State<App>,OriginalUri(uri):OriginalUri,Query(query):Query<HashMap<String,String>>)->Result<Json<Value>,ApiError>{
    perform(&app,uri.path().trim_start_matches("/vocab/").into(),json!(query),false).await
}

async fn edit(State(app):State<App>,OriginalUri(uri):OriginalUri,body:Bytes)->Result<Json<Value>,ApiError>{
    let data:Value=serde_json::from_slice(&body).ok().filter(Value::is_object).ok_or_else(||ApiError::new(StatusCode::BAD_REQUEST,"bad json"))?;
    perform(&app,uri.path().trim_start_matches("/vocab/").into(),data,true).await
}

pub fn routes()->Router<App>{
    let mut routes=Router::new();
    for path in ["/vocab/packs","/vocab/terms","/vocab/profiles"] {routes=routes.route(path,get(view).post(edit));}
    for path in ["/vocab/profile","/vocab/assignments","/vocab/runs","/vocab/run"] {routes=routes.route(path,get(view));}
    for path in ["/vocab/packs/update","/vocab/packs/delete","/vocab/terms/update","/vocab/terms/delete","/vocab/profiles/delete","/vocab/profiles/workspace","/vocab/profiles/packs","/vocab/profiles/packs/remove","/vocab/assign","/vocab/unassign"] {routes=routes.route(path,post(edit));}
    routes
}
