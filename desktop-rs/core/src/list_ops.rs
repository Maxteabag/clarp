//! Ordered list mutations shared by every core model. A Qt adapter replays
//! them onto its row mirror between the matching begin/end notifications, so
//! views never see a half-applied change and never re-enter core state.

#[derive(Debug, Clone, PartialEq)]
pub enum ListOp<T, R, S> {
    Reset(Vec<T>),
    Insert { at: usize, rows: Vec<T> },
    Remove { at: usize, count: usize },
    /// QList::move semantics: the row at `from` ends up at index `to`.
    Move { from: usize, to: usize },
    /// Replace rows `first..first + items.len()`. `roles` empty means nothing
    /// visible changed and no dataChanged is needed.
    Update { first: usize, items: Vec<T>, roles: Vec<R> },
    Signal(S),
}

/// Apply ops to a mirror of the rows, exactly as a Qt adapter does.
pub fn replay<T: Clone, R, S>(mirror: &mut Vec<T>, ops: &[ListOp<T, R, S>]) {
    for op in ops {
        match op {
            ListOp::Reset(rows) => *mirror = rows.clone(),
            ListOp::Insert { at, rows } => {
                mirror.splice(*at..*at, rows.iter().cloned());
            }
            ListOp::Remove { at, count } => {
                mirror.drain(*at..*at + *count);
            }
            ListOp::Move { from, to } => {
                let row = mirror.remove(*from);
                mirror.insert(*to, row);
            }
            ListOp::Update { first, items, .. } => {
                for (offset, item) in items.iter().enumerate() {
                    mirror[first + offset] = item.clone();
                }
            }
            ListOp::Signal(_) => {}
        }
    }
}

/// Qt's beginMoveRows destination for a QList::move(from, to).
pub fn qt_move_destination(from: usize, to: usize) -> usize {
    if from < to { to + 1 } else { to }
}
