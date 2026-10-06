use clarp_core::mention::{Active, Candidate, active, complete, rank, target};

fn candidates() -> Vec<Candidate> {
    vec![
        Candidate { session: "dagger".into(), name: "Dagger".into(), activity: 10 },
        Candidate { session: "dg-helper".into(), name: "Dora Grey".into(), activity: 30 },
        Candidate { session: "mike".into(), name: "Mike".into(), activity: 20 },
        Candidate { session: "mikael".into(), name: "Mikael".into(), activity: 5 },
    ]
}

#[test]
fn an_at_sign_at_a_word_start_opens_a_mention() {
    assert_eq!(active("@", 1), Some(Active { start: 0, end: 1, query: String::new() }));
    assert_eq!(active("ask @Da please", 7), Some(Active { start: 4, end: 7, query: "Da".into() }));
    assert_eq!(active("ask @Dagger please", 7), Some(Active { start: 4, end: 11, query: "Da".into() }), "the token runs on past the cursor");
    assert_eq!(active("mail me@host", 12), None, "inside a word it is an address");
    assert_eq!(active("ask @Da please", 13), None, "the cursor left the token");
    assert_eq!(active("æøå @ø", "æøå @ø".len()), Some(Active { start: 7, end: 10, query: "ø".into() }), "byte offsets");
}

#[test]
fn ranking_is_fuzzy_prefix_first_then_recent() {
    let names = |query: &str| rank(&candidates(), query, 8).into_iter().map(|c| c.name).collect::<Vec<_>>();
    assert_eq!(names("mi"), ["Mike", "Mikael"], "both prefixes; the more recent first");
    assert_eq!(names("mike"), ["Mike", "Mikael"], "the exact name first, a scattered match after");
    assert_eq!(names("dg"), ["Dora Grey", "Dagger"], "initials beat a scattered subsequence");
    assert_eq!(names("agg"), ["Dagger"]);
    assert_eq!(names("").len(), 4, "an empty query lists everyone, recent first");
    assert_eq!(names("")[0], "Dora Grey");
    assert!(names("zz").is_empty());
}

#[test]
fn completing_inserts_the_name_and_a_space() {
    let mention = active("ask @Da please", 7).unwrap();
    assert_eq!(complete("ask @Da please", &mention, "Dagger"), ("ask @Dagger please".to_owned(), 12));
    let mention = active("@mi", 3).unwrap();
    assert_eq!(complete("@mi", &mention, "Mike"), ("@Mike ".to_owned(), 6));
}

#[test]
fn the_target_is_the_first_mention_of_a_known_agent() {
    let all = candidates();
    assert_eq!(target("@Mike status please", &all).map(|c| c.session.as_str()), Some("mike"));
    assert_eq!(target("hey @dora grey, and @Mike", &all).map(|c| c.session.as_str()), Some("dg-helper"), "names with spaces, any case");
    assert_eq!(target("@Mikael: go", &all).map(|c| c.session.as_str()), Some("mikael"), "the longest name wins");
    assert_eq!(target("@Mikey hi", &all), None, "a name must end at a word boundary");
    assert_eq!(target("mail me@Mike", &all), None);
    assert_eq!(target("no mention", &all), None);
}
