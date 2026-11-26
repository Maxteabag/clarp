//! A wide table's columns in the chat (transcript.slint `TableFit`).
//!
//! The first column stays put, so every row keeps its label. After it the
//! table shows whole columns only, from `first` on, as many as fit; those
//! shown share the room that is left, so the table reaches its block's
//! right edge and no column is ever cut. Scrolling sideways moves `first`
//! a column at a time; a page moves it past the last column shown.
//!
//! Widths are in logical pixels: a column wants its characters (view.rs
//! `table_columns`) times `char`, plus `pad`.

use slint::{ComponentHandle, Model, ModelRc, VecModel};

fn wants(columns: &[i32], char: f32, pad: f32) -> Vec<f32> {
    columns.iter().map(|c| (*c).max(1) as f32 * char + pad).collect()
}

/// The last column shown when the scrolled part starts at `first`: as many
/// whole columns as fit beside the first one, and always `first` itself.
pub fn last(columns: &[i32], first: i32, room: f32, char: f32, pad: f32) -> i32 {
    let count = columns.len();
    if count < 2 {
        return count as i32 - 1;
    }
    let want = wants(columns, char, pad);
    let first = (first.max(1) as usize).min(count - 1);
    let mut used = want[0] + want[first];
    let mut last = first;
    while last + 1 < count && used + want[last + 1] <= room {
        last += 1;
        used += want[last];
    }
    last as i32
}

/// The furthest `first` can go: the first one at which the last column
/// shows. 1 when the whole table fits.
pub fn end(columns: &[i32], room: f32, char: f32, pad: f32) -> i32 {
    let count = columns.len() as i32;
    (1..count).find(|first| last(columns, *first, room, char, pad) == count - 1).unwrap_or(1).max(1)
}

/// Each column's width with the scrolled part starting at `first`; a
/// column out of view is 0. Those shown fill `room` exactly, the room left
/// shared by what each wants; too narrow for even two, they shrink alike.
pub fn widths(columns: &[i32], first: i32, room: f32, char: f32, pad: f32) -> Vec<f32> {
    let count = columns.len();
    let room = room.max(0.0);
    if count == 0 {
        return Vec::new();
    }
    if count == 1 {
        return vec![room];
    }
    let want = wants(columns, char, pad);
    let first = (first.max(1) as usize).min(count - 1);
    let last = last(columns, first as i32, room, char, pad) as usize;
    let shown = |k: usize| k == 0 || (first..=last).contains(&k);
    let used: f32 = (0..count).filter(|k| shown(*k)).map(|k| want[k]).sum();
    (0..count)
        .map(|k| {
            if !shown(k) {
                0.0
            } else if used <= room {
                want[k] + (room - used) * want[k] / used
            } else {
                want[k] * room / used
            }
        })
        .collect()
}

fn ints(model: &ModelRc<i32>) -> Vec<i32> {
    model.iter().collect()
}

pub fn wire(window: &crate::AppWindow) {
    let fit = window.global::<crate::TableFit>();
    fit.on_widths(|columns, first, room, char, pad| ModelRc::new(VecModel::from(widths(&ints(&columns), first, room, char, pad))));
    fit.on_last(|columns, first, room, char, pad| last(&ints(&columns), first, room, char, pad));
    fit.on_end(|columns, room, char, pad| end(&ints(&columns), room, char, pad));
}

#[cfg(test)]
mod tests {
    use super::*;

    // Nine columns wanting 100 px each (10 characters at 10 px).
    fn nine() -> Vec<i32> {
        vec![10; 9]
    }

    #[test]
    fn a_table_that_fits_shows_every_column_and_fills_its_block() {
        let w = widths(&[5, 10, 5], 1, 400.0, 10.0, 0.0);
        assert_eq!(end(&[5, 10, 5], 400.0, 10.0, 0.0), 1);
        assert!((w.iter().sum::<f32>() - 400.0).abs() < 0.01, "{w:?}");
        assert!(w[1] > w[0], "the wider column gets more of the room: {w:?}");
    }

    #[test]
    fn a_wide_table_keeps_its_first_column_and_shows_only_whole_columns_to_the_edge() {
        // Room for the first column and 3.5 more: 3 whole columns show and
        // share the half that is left.
        let room = 450.0;
        for first in 1..=end(&nine(), room, 10.0, 0.0) {
            let w = widths(&nine(), first, room, 10.0, 0.0);
            assert!(w[0] > 0.0, "the first column always shows");
            let shown: Vec<usize> = (0..9).filter(|k| w[*k] > 0.0).collect();
            assert_eq!(shown.len(), 4, "first {first}: {w:?}");
            assert!(shown.iter().all(|k| w[*k] >= 100.0), "no column narrower than it wants: {w:?}");
            assert!((w.iter().sum::<f32>() - room).abs() < 0.01, "reaches the right edge: {w:?}");
        }
        assert_eq!(end(&nine(), room, 10.0, 0.0), 6);
        assert_eq!(last(&nine(), 6, room, 10.0, 0.0), 8, "at its end the last column shows");
        assert_eq!(last(&nine(), 1, room, 10.0, 0.0), 3);
    }

    #[test]
    fn too_narrow_for_two_columns_they_share_the_room() {
        let w = widths(&[20, 20, 20], 1, 300.0, 10.0, 0.0);
        assert_eq!(w[2], 0.0);
        assert!((w[0] + w[1] - 300.0).abs() < 0.01, "{w:?}");
        assert_eq!(last(&[20, 20, 20], 99, 300.0, 10.0, 0.0), 2, "first past the end is the last column");
        assert!(widths(&[], 1, 300.0, 10.0, 0.0).is_empty());
        assert_eq!(widths(&[4], 1, 300.0, 10.0, 0.0), vec![300.0]);
    }
}
