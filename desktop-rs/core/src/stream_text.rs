//! A streaming message's text: where its committed blocks end and how far
//! the paced reveal moves per frame.

pub fn commit_point(_text: &str) -> usize {
    0
}

pub fn next_reveal(shown: usize, _text: &str) -> usize {
    shown
}
