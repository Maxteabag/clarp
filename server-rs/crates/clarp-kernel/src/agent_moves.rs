//! An agent's move mark and the refusal shared by its turn entry points.

use serde::{Deserialize, Serialize};

pub const MOVED: &str = "agent_moved";
pub const CONTINUATION_ERROR: &str =
    "The agent moved to another Host; this turn does not continue here";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Target {
    pub host: String,
    pub id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mark {
    pub to: Target,
    pub at: i64,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Receipt {
    pub changed: bool,
    pub moved: Option<Mark>,
}
