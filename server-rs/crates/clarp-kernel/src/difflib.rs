//! Python's `difflib`, as far as a unified diff of two lists of lines.
//!
//! The Python Host counts the lines an edit adds and removes, and shows the first of them,
//! with `difflib.unified_diff`. Another way of matching two texts finds another diff of the
//! same edit, with other counts, so this is that one: `SequenceMatcher` with its defaults.
//! Nothing is junk; in a text of 200 lines or more, a line that makes up more than a
//! hundredth of it is "popular" and no match starts from it.
//!
//! Held to Python's own answers by `tests/fixtures/live-vectors.json`.

use std::collections::HashMap;

/// A run of lines the two texts share: `size` lines from line `a` of the first and line `b`
/// of the second. Ordered as Python orders its tuples of three.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Match {
    a: usize,
    b: usize,
    size: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tag {
    Replace,
    Delete,
    Insert,
    Equal,
}

/// What to do with lines `i1..i2` of the first text to get lines `j1..j2` of the second.
type Step = (Tag, usize, usize, usize, usize);

struct Matcher<'a> {
    a: &'a [&'a str],
    b: &'a [&'a str],
    /// Where each line of the second text is, for the lines a match may start from.
    places: HashMap<&'a str, Vec<usize>>,
}

impl<'a> Matcher<'a> {
    fn new(a: &'a [&'a str], b: &'a [&'a str]) -> Self {
        let mut places: HashMap<&str, Vec<usize>> = HashMap::new();
        for (at, line) in b.iter().enumerate() {
            places.entry(line).or_default().push(at);
        }
        if b.len() >= 200 {
            let popular = b.len() / 100 + 1;
            places.retain(|_, at| at.len() <= popular);
        }
        Self { a, b, places }
    }

    /// The longest run the two ranges share, the earliest in the first text among the
    /// longest, and of those the earliest in the second.
    fn longest(&self, alo: usize, ahi: usize, blo: usize, bhi: usize) -> Match {
        let (mut best_a, mut best_b, mut best) = (alo, blo, 0);
        // How long the run is that ends at each line of the second text, for the line of
        // the first text before this one.
        let mut runs: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut next: HashMap<usize, usize> = HashMap::new();
            if let Some(places) = self.places.get(self.a[i]) {
                for &j in places {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let before = if j == 0 {
                        0
                    } else {
                        runs.get(&(j - 1)).copied().unwrap_or(0)
                    };
                    let run = before + 1;
                    next.insert(j, run);
                    if run > best {
                        (best_a, best_b, best) = (i + 1 - run, j + 1 - run, run);
                    }
                }
            }
            runs = next;
        }
        // A popular line starts no match, and is still part of one that reaches it.
        while best_a > alo && best_b > blo && self.a[best_a - 1] == self.b[best_b - 1] {
            (best_a, best_b, best) = (best_a - 1, best_b - 1, best + 1);
        }
        while best_a + best < ahi
            && best_b + best < bhi
            && self.a[best_a + best] == self.b[best_b + best]
        {
            best += 1;
        }
        Match {
            a: best_a,
            b: best_b,
            size: best,
        }
    }

    /// Every run the texts share, in order, with runs that touch joined, and an empty one
    /// at the end of both.
    fn blocks(&self) -> Vec<Match> {
        let mut waiting = vec![(0, self.a.len(), 0, self.b.len())];
        let mut found = Vec::new();
        while let Some((alo, ahi, blo, bhi)) = waiting.pop() {
            let run = self.longest(alo, ahi, blo, bhi);
            if run.size == 0 {
                continue;
            }
            found.push(run);
            if alo < run.a && blo < run.b {
                waiting.push((alo, run.a, blo, run.b));
            }
            if run.a + run.size < ahi && run.b + run.size < bhi {
                waiting.push((run.a + run.size, ahi, run.b + run.size, bhi));
            }
        }
        found.sort();
        let mut joined = Vec::new();
        let mut held = Match {
            a: 0,
            b: 0,
            size: 0,
        };
        for run in found {
            if held.a + held.size == run.a && held.b + held.size == run.b {
                held.size += run.size;
            } else {
                if held.size > 0 {
                    joined.push(held);
                }
                held = run;
            }
        }
        if held.size > 0 {
            joined.push(held);
        }
        joined.push(Match {
            a: self.a.len(),
            b: self.b.len(),
            size: 0,
        });
        joined
    }

    fn steps(&self) -> Vec<Step> {
        let (mut i, mut j) = (0, 0);
        let mut steps = Vec::new();
        for run in self.blocks() {
            let tag = match (i < run.a, j < run.b) {
                (true, true) => Some(Tag::Replace),
                (true, false) => Some(Tag::Delete),
                (false, true) => Some(Tag::Insert),
                (false, false) => None,
            };
            if let Some(tag) = tag {
                steps.push((tag, i, run.a, j, run.b));
            }
            (i, j) = (run.a + run.size, run.b + run.size);
            if run.size > 0 {
                steps.push((Tag::Equal, run.a, i, run.b, j));
            }
        }
        steps
    }

    /// The steps in groups: each a place where the texts differ, with `context` equal lines
    /// around it.
    fn groups(&self, context: usize) -> Vec<Vec<Step>> {
        let mut steps = self.steps();
        if steps.is_empty() {
            steps.push((Tag::Equal, 0, 1, 0, 1));
        }
        if let Some(first) = steps.first_mut() {
            let (tag, i1, i2, j1, j2) = *first;
            if tag == Tag::Equal {
                *first = (
                    tag,
                    i1.max(i2.saturating_sub(context)),
                    i2,
                    j1.max(j2.saturating_sub(context)),
                    j2,
                );
            }
        }
        if let Some(last) = steps.last_mut() {
            let (tag, i1, i2, j1, j2) = *last;
            if tag == Tag::Equal {
                *last = (tag, i1, i2.min(i1 + context), j1, j2.min(j1 + context));
            }
        }
        let mut groups = Vec::new();
        let mut group: Vec<Step> = Vec::new();
        for (tag, mut i1, i2, mut j1, j2) in steps {
            // A long stretch without a change ends one group and begins the next.
            if tag == Tag::Equal && i2 - i1 > context + context {
                group.push((tag, i1, i2.min(i1 + context), j1, j2.min(j1 + context)));
                groups.push(std::mem::take(&mut group));
                i1 = i1.max(i2.saturating_sub(context));
                j1 = j1.max(j2.saturating_sub(context));
            }
            group.push((tag, i1, i2, j1, j2));
        }
        let only_equal = group.len() == 1 && group.first().is_some_and(|step| step.0 == Tag::Equal);
        if !group.is_empty() && !only_equal {
            groups.push(group);
        }
        groups
    }
}

/// A range of lines as a unified diff names it in the line that begins a group.
fn range(start: usize, stop: usize) -> String {
    let length = stop - start;
    match length {
        1 => (start + 1).to_string(),
        0 => format!("{start},0"),
        _ => format!("{},{length}", start + 1),
    }
}

/// `difflib.unified_diff(a, b, lineterm="", n=context)`: the lines of the diff. Two texts
/// that are the same have none; others begin with the two lines that would name the files
/// (`--- ` and `+++ `, which name none here).
pub fn unified(a: &[&str], b: &[&str], context: usize) -> Vec<String> {
    let matcher = Matcher::new(a, b);
    let mut lines = Vec::new();
    for group in matcher.groups(context) {
        let (Some(first), Some(last)) = (group.first(), group.last()) else {
            continue;
        };
        if lines.is_empty() {
            lines.push("--- ".to_string());
            lines.push("+++ ".to_string());
        }
        lines.push(format!(
            "@@ -{} +{} @@",
            range(first.1, last.2),
            range(first.3, last.4)
        ));
        for &(tag, i1, i2, j1, j2) in &group {
            if tag == Tag::Equal {
                lines.extend(a[i1..i2].iter().map(|line| format!(" {line}")));
                continue;
            }
            if matches!(tag, Tag::Replace | Tag::Delete) {
                lines.extend(a[i1..i2].iter().map(|line| format!("-{line}")));
            }
            if matches!(tag, Tag::Replace | Tag::Insert) {
                lines.extend(b[j1..j2].iter().map(|line| format!("+{line}")));
            }
        }
    }
    lines
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn two_texts_that_are_the_same_have_no_diff() {
        assert!(unified(&[], &[], 1).is_empty());
        assert!(unified(&["a", "b"], &["a", "b"], 1).is_empty());
    }

    #[test]
    fn a_changed_line_is_shown_with_one_line_around_it() {
        let diff = unified(&["a", "b", "c", "d"], &["a", "x", "c", "d"], 1);
        assert_eq!(
            diff,
            ["--- ", "+++ ", "@@ -1,3 +1,3 @@", " a", "-b", "+x", " c"]
        );
        // Only added lines, and only removed ones.
        assert_eq!(
            unified(&[], &["a"], 1),
            ["--- ", "+++ ", "@@ -0,0 +1 @@", "+a"]
        );
        assert_eq!(
            unified(&["a"], &[], 1),
            ["--- ", "+++ ", "@@ -1 +0,0 @@", "-a"]
        );
    }
}
