//! The Host's mirror of a Codex thread goal (contract 5).

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Active,
    Paused,
    Blocked,
    UsageLimited,
    BudgetLimited,
    Complete,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Goal {
    objective: String,
    pub status: Status,
    token_budget: Option<i64>,
    tokens_used: i64,
    time_used_seconds: i64,
    created_at: i64,
    updated_at: i64,
}

impl Goal {
    /// A provider result or update. A malformed update cannot erase the current mirror.
    pub fn from_codex(raw: &Value, previous: Option<&Self>, now_ms: i64) -> Option<Self> {
        let fields = raw.as_object()?;
        let text = |key| match fields.get(key) {
            None | Some(Value::Null) => Some(""),
            Some(value) => value.as_str(),
        };
        let count = |key| match fields.get(key) {
            None | Some(Value::Null) => Some(0),
            Some(value) => value.as_i64(),
        };
        let status = match crate::python::strip(text("status")?) {
            "" | "active" => Status::Active,
            "paused" => Status::Paused,
            "blocked" => Status::Blocked,
            "usageLimited" | "usage_limited" => Status::UsageLimited,
            "budgetLimited" | "budget_limited" => Status::BudgetLimited,
            "complete" => Status::Complete,
            _ => return None,
        };
        let token_budget = match fields.get("tokenBudget") {
            None | Some(Value::Null) => None,
            Some(value) => Some(value.as_i64()?),
        };
        Some(Self {
            objective: text("objective")?.to_owned(),
            status,
            token_budget,
            tokens_used: count("tokensUsed")?,
            time_used_seconds: count("timeUsedSeconds")?,
            created_at: epoch_ms(&raw["createdAt"])
                .or_else(|| previous.map(|goal| goal.created_at))
                .unwrap_or(now_ms),
            updated_at: epoch_ms(&raw["updatedAt"]).unwrap_or(now_ms),
        })
    }

    pub fn public(&self) -> Value {
        json!({
            "objective": self.objective,
            "status": self.status,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "time_used_seconds": self.time_used_seconds,
            "native": true,
            "backend": "codex",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })
    }
}

fn epoch_ms(value: &Value) -> Option<i64> {
    let n = value.as_i64().or_else(|| {
        let n = crate::python::int(value.as_str()?)?;
        i64::try_from(n).ok()
    })?;
    match n {
        1..100_000_000_000 => Some(n * 1_000),
        100_000_000_000.. => Some(n),
        _ => None,
    }
}
