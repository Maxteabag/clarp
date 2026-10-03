//! A streaming message's text: where its committed blocks end (so only the
//! tail after them is parsed again as it grows) and how far the paced
//! reveal moves per frame.

/// The byte index after the last blank line outside a code fence: the
/// text before it is whole blocks that later text cannot change.
pub fn commit_point(text: &str) -> usize {
    let mut fence: Option<&str> = None;
    let mut point = 0;
    let mut offset = 0;
    let mut previous_blank = false;
    for line in text.split_inclusive('\n') {
        let end = offset + line.len();
        let trimmed = line.trim_start();
        let marker = if trimmed.starts_with("```") { Some("```") } else if trimmed.starts_with("~~~") { Some("~~~") } else { None };
        match (fence, marker) {
            (None, Some(m)) => fence = Some(m),
            (Some(open), Some(m)) if open == m => fence = None,
            _ => {}
        }
        let blank = line.trim().is_empty() && line.ends_with('\n');
        // A blank line after a non-blank one closes the block before it.
        if blank && fence.is_none() && !previous_blank && offset > 0 {
            point = end;
        }
        previous_blank = blank;
        offset = end;
    }
    point
}

/// Where the reveal goes next from `shown` bytes of `text`: a sixth of
/// what is pending (at least four bytes), ending at a word, so text that
/// arrives in bursts flows out within a few frames.
pub fn next_reveal(shown: usize, text: &str) -> usize {
    if shown >= text.len() {
        return text.len();
    }
    let pending = text.len() - shown;
    let mut target = shown + (pending.div_ceil(6)).max(4);
    if target >= text.len() {
        return text.len();
    }
    while !text.is_char_boundary(target) {
        target += 1;
    }
    match text[target..].find(char::is_whitespace) {
        Some(gap) if gap <= 16 => target + gap,
        // The last word: all of it.
        None if text.len() - target <= 16 => text.len(),
        // A very long word: back to the space before it, or through it.
        _ => text[shown..target].rfind(char::is_whitespace).map(|i| shown + i).filter(|i| *i > shown).unwrap_or(target),
    }
}
