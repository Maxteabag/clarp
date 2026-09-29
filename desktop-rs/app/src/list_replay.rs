//! Replays a core `ListOp` onto a cxx-qt QAbstractListModel's `rows` mirror
//! between the matching begin/end calls. Expands inside an adapter method
//! whose bridge declares the begin*/end* inherits, `index` and `data_changed`.

macro_rules! replay_list_op {
    ($this:ident, $op:expr, $role_id:expr, $emit:ident) => {{
        use clarp_core::list_ops::{ListOp, qt_move_destination};
        let root = cxx_qt_lib::QModelIndex::default();
        match $op {
            ListOp::Reset(rows) => unsafe {
                $this.as_mut().begin_reset_model();
                $this.as_mut().rust_mut().rows = rows;
                $this.as_mut().end_reset_model();
            },
            ListOp::Insert { at, rows } => {
                if !rows.is_empty() {
                    let last = (at + rows.len() - 1) as i32;
                    unsafe { $this.as_mut().begin_insert_rows(&root, at as i32, last) };
                    $this.as_mut().rust_mut().rows.splice(at..at, rows);
                    unsafe { $this.as_mut().end_insert_rows() };
                }
            }
            ListOp::Remove { at, count } => {
                if count > 0 {
                    unsafe { $this.as_mut().begin_remove_rows(&root, at as i32, (at + count - 1) as i32) };
                    $this.as_mut().rust_mut().rows.drain(at..at + count);
                    unsafe { $this.as_mut().end_remove_rows() };
                }
            }
            ListOp::Move { from, to } => {
                if from != to {
                    let destination = qt_move_destination(from, to) as i32;
                    let moved = unsafe {
                        $this.as_mut().begin_move_rows(&root, from as i32, from as i32, &root, destination)
                    };
                    let row = $this.as_mut().rust_mut().rows.remove(from);
                    $this.as_mut().rust_mut().rows.insert(to, row);
                    if moved {
                        unsafe { $this.as_mut().end_move_rows() };
                    } else {
                        eprintln!("list model: Qt rejected a move {from} -> {to}; views may be stale");
                    }
                }
            }
            ListOp::Update { first, items, roles } => {
                let count = items.len();
                for (offset, item) in items.into_iter().enumerate() {
                    $this.as_mut().rust_mut().rows[first + offset] = item;
                }
                if count > 0 && !roles.is_empty() {
                    let top = $this.index(first as i32, 0, &root);
                    let bottom = $this.index((first + count - 1) as i32, 0, &root);
                    let mut ids = cxx_qt_lib::QList::<i32>::default();
                    for role in roles {
                        ids.append($role_id(role));
                    }
                    $this.as_mut().data_changed(&top, &bottom, &ids);
                }
            }
            ListOp::Signal(signal) => $this.$emit(signal),
        }
    }};
}

pub(crate) use replay_list_op;
