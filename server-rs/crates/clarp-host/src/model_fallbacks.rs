//! Retry a pure, session-free question. Publication and conversation dispatch stay
//! with the caller, after it has an accepted answer.
use crate::edge::App;
use crate::once::Unanswered;
use clarp_kernel::janitors::fallbacks::{Pick, choices, retryable};
use serde_json::json;
use std::future::Future;
use std::time::Duration;

/// What this desk keeps, declared once (`kept.rs`). A receipt for each attempt, never let go,
/// read by key alone and so not at the start: this desk reads only that one is there (an
/// attempt claimed is never made twice), so whatever is there is one of its own.
pub(crate) const KEPT: &[crate::kept::Kind] =
    &[crate::kept::Kind::book(KIND, crate::kept::Key::Plain, crate::kept::any).when_read()];

const KIND: &str = "model-fallback-attempt";
#[derive(Default)]
pub struct Desk(tokio::sync::Mutex<()>);

#[derive(Debug)]
pub(crate) struct Failure {
    pub code: &'static str,
    /// What a person is told. A failed tool's own words only where they hold none of its
    /// question.
    pub detail: String,
    /// What a failed tool's failure is, read once from its raw words and its exit: whether the
    /// chain moves on and the keeper's code come from here, never from `detail`.
    pub facts: Option<Box<clarp_kernel::failures::CliFailure>>,
    reason: &'static str,
    retry: bool,
}
impl Failure {
    /// A failure in the Host's own words.
    pub fn model(code: &'static str, detail: impl Into<String>) -> Self {
        let detail = detail.into();
        Self {
            code,
            retry: retryable(&detail),
            detail,
            facts: None,
            reason: "ProviderFailure",
        }
    }
    pub fn because(mut self, reason: &'static str) -> Self {
        self.reason = reason;
        self
    }

    pub fn routing(error: Unanswered) -> Self {
        let reason = match &error {
            Unanswered::NotStarted(_) => "FileNotFoundError",
            Unanswered::PromptTooLarge(_) => "PromptTooLarge",
            Unanswered::PromptNotEncodable(_) => "PromptNotEncodable",
            Unanswered::TimedOut | Unanswered::RoutingTimeout(_) => "TimeoutExpired",
            Unanswered::NoAnswer(_) => "ValueError",
            _ => "RuntimeError",
        };
        Self::runner(error).because(reason)
    }

    pub fn runner(error: Unanswered) -> Self {
        let code = match &error {
            Unanswered::TimedOut | Unanswered::RoutingTimeout(_) => "timeout",
            Unanswered::NotStarted(_) | Unanswered::NoSuchTool(_) => "tool_unavailable",
            Unanswered::Failed(_)
            | Unanswered::PromptNotEncodable(_)
            | Unanswered::PromptTooLarge(_) => "tool_failed",
            Unanswered::NoAnswer(_) => "parse_error",
        };
        let origin = match &error {
            Unanswered::PromptTooLarge(_) => "PromptTooLarge",
            Unanswered::PromptNotEncodable(_) => "PromptNotEncodable",
            Unanswered::RoutingTimeout(_) | Unanswered::TimedOut => "TimeoutExpired",
            _ => "ProviderFailure",
        };
        let mut failure = Self::model(code, error.words());
        if let Unanswered::Failed(facts) = error {
            failure.retry = facts.moves_on;
            failure.facts = Some(Box::new(facts));
        }
        failure.because(origin)
    }
    fn control(detail: &str) -> Self {
        Self {
            code: "cancelled",
            detail: detail.into(),
            facts: None,
            reason: "Cancelled",
            retry: false,
        }
    }
}

async fn current(app: &App, run: &str) -> bool {
    app.janitors
        .read(app, |book, world, at| book.is_current(run, world, at))
        .await
        .unwrap_or(false)
}

async fn guarded<T>(
    app: &App,
    run: &str,
    call: impl Future<Output = Result<T, Failure>>,
) -> Result<T, Failure> {
    tokio::pin!(call);
    let mut tick = tokio::time::interval(Duration::from_millis(250));
    tick.tick().await;
    loop {
        tokio::select! {
            result=&mut call=>return result,
            _=tick.tick()=>if !current(app,run).await{return Err(Failure::control("request is no longer current"))},
        }
    }
}

pub(crate) async fn execute<T, F, Fut>(
    app: &App,
    run: &str,
    suffix: &str,
    primary: Pick,
    mut invoke: F,
) -> Result<(T, Pick), Failure>
where
    F: FnMut(Pick) -> Fut,
    Fut: Future<Output = Result<T, Failure>>,
{
    let context = app
        .janitors
        .read(app, |book, world, now| {
            let row = book.runs.get(run)?;
            book.is_current(run, world, now).then(|| {
                (
                    row.agent_id.clone(),
                    row.configuration
                        .get("effective_chain")
                        .cloned()
                        .unwrap_or_default(),
                )
            })
        })
        .await
        .ok()
        .flatten();
    let (agent, chain) = context.ok_or_else(|| Failure::control("request is no longer current"))?;
    let (primary, alternates) = choices(primary, &chain);
    let mut failure = match guarded(app, run, invoke(primary.clone())).await {
        Ok(value) => return Ok((value, primary)),
        Err(error) => error,
    };
    for (index, pick) in alternates {
        if !failure.retry {
            return Err(failure);
        }
        let request = if suffix.is_empty() {
            run.to_owned()
        } else {
            format!("{run}:{suffix}")
        };
        let key = json!([agent, request, index]).to_string();
        let mut receipt = json!({"agent_id":agent,"request_id":request,"attempt":index,"backend":pick.backend,"model":pick.model,"effort":pick.effort,
            "status":"running","reason":failure.reason,"result_json":null,"started_at":app.clock.now_ms(),"finished_at":null,"runtime_id":null});
        let unavailable = || Failure::control("fallback receipt store unavailable");
        {
            let _writer = app.fallbacks.0.lock().await;
            if !current(app, run).await {
                return Err(Failure::control("fallback configuration changed"));
            }
            if app
                .fleet
                .kept_book(KIND, key.clone())
                .await
                .map_err(|_| unavailable())?
                .is_some()
            {
                return Err(Failure::control(
                    "fallback invocation already claimed; inspect its receipt",
                ));
            }
            app.fleet
                .keep(KIND, key.clone(), Some(receipt.to_string().into_bytes()))
                .await
                .map_err(|_| unavailable())?;
        }
        let mut result = guarded(app, run, invoke(pick.clone())).await;
        if result.is_ok() && !current(app, run).await {
            result = Err(Failure::control("request is no longer current"));
        }
        receipt["finished_at"] = json!(app.clock.now_ms());
        receipt["status"] = json!(match &result {
            Ok(_) => "completed",
            Err(e) if e.retry => "failed",
            Err(_) => "cancelled",
        });
        app.fleet
            .keep(KIND, key, Some(receipt.to_string().into_bytes()))
            .await
            .map_err(|_| unavailable())?;
        match result {
            Ok(value) => return Ok((value, pick)),
            Err(error) => failure = error,
        }
    }
    Err(failure)
}
