//! Classification and placement of a waiting message carrying the user's word (issue 481).

/// Only the origin or a recorded answer the Host has already verified supplies authority.
pub fn is_user_word(origin: &str, answered_by: &str, choice: &str) -> bool {
    matches!(crate::python::strip(origin), "user" | "oracle")
        || ((answered_by == "user" || answered_by.starts_with("agent:"))
            && matches!(choice, "accepted" | "rejected" | "answered"))
}

/// The user's word waits behind every protected item already in the queue.
pub fn slot(protected: impl IntoIterator<Item = bool>) -> usize {
    protected
        .into_iter()
        .enumerate()
        .filter_map(|(index, protects)| protects.then_some(index + 1))
        .last()
        .unwrap_or(0)
}
