//! The app's language: which device languages map to which shipped one, the
//! catalog `tr!` reads, and the formatters.

use chrono::{NaiveDate, NaiveTime};
use clarp_core::i18n::{self, format};

#[test]
fn device_languages_map_to_shipped_ones() {
    assert_eq!(i18n::supported("nb_NO.UTF-8"), Some("nb"));
    assert_eq!(i18n::supported("nn-NO"), Some("nb"), "nynorsk reads bokmål");
    assert_eq!(i18n::supported("no"), Some("nb"));
    assert_eq!(i18n::supported("zh-CN"), Some("zh-Hans"));
    assert_eq!(i18n::supported("zh_SG"), Some("zh-Hans"));
    assert_eq!(i18n::supported("zh-Hans-HK"), Some("zh-Hans"));
    assert_eq!(i18n::supported("zh-TW"), None, "traditional Chinese is not shipped");
    assert_eq!(i18n::supported("zh-Hant"), None);
    assert_eq!(i18n::supported("de-DE"), None);
    assert_eq!(i18n::supported("C"), None);
    assert_eq!(i18n::from_preferred(["de-DE", "zh-TW", "fr-CA", "nb"]), "fr", "the first shipped one wins");
    assert_eq!(i18n::from_preferred(["de-DE"]), "en", "nothing shipped: English");
}

#[test]
fn a_chosen_language_wins_over_the_device() {
    assert_eq!(i18n::resolve("hi"), "hi");
    assert_eq!(i18n::resolve("zh-Hans"), "zh-Hans");
}

#[test]
fn plural_rules_match_the_catalog_headers() {
    let built = i18n::catalog_plural_forms();
    assert_eq!(built.len(), i18n::PLURAL_FORMS.len(), "every catalog states its plural rule: {built:?}");
    for (tag, rule) in i18n::PLURAL_FORMS {
        assert!(built.contains(&(tag, rule)), "{tag}'s catalog says {rule}: {built:?}");
    }
    assert_eq!((i18n::plural_index("fr", 0), i18n::plural_index("fr", 1), i18n::plural_index("fr", 2)), (0, 0, 1));
    assert_eq!((i18n::plural_index("nb", 0), i18n::plural_index("nb", 1)), (1, 0));
    assert_eq!(i18n::plural_index("zh-Hans", 5), 0);
}

#[test]
fn rust_text_reads_the_same_catalog_as_slint() {
    assert_eq!(i18n::translate_in("nb", "", "Language", "", None, &[]), "Språk");
    assert_eq!(i18n::translate_in("hi", "", "Language", "", None, &[]), "भाषा");
    assert_eq!(i18n::translate_in("en", "", "Language", "", None, &[]), "Language");
    assert_eq!(i18n::translate_in("nb", "", "Not in the catalog", "", None, &[]), "Not in the catalog");
    assert_eq!(i18n::translate_in("fr", "", "System ({})", "", None, &[&"Français"]), "Système (Français)");
    let ago = |tag, n| i18n::translate_in(tag, "", "{n} day ago", "{n} days ago", Some(n), &[]);
    assert_eq!((ago("en", 1), ago("en", 3)), ("1 day ago".to_owned(), "3 days ago".to_owned()));
    assert_eq!((ago("nb", 1), ago("nb", 3)), ("for 1 dag siden".to_owned(), "for 3 dager siden".to_owned()));
    assert_eq!(ago("zh-Hans", 3), "3 天前");
}

#[test]
fn tr_follows_the_current_language() {
    i18n::set_current("es");
    assert_eq!(i18n::current(), "es");
    assert_eq!(clarp_core::tr!("Language"), "Idioma");
    assert_eq!(clarp_core::tr!("in {n} hour" | "in {n} hours" % 2), "dentro de 2 horas");
    i18n::set_current("xx");
    assert_eq!(i18n::current(), "en", "an unknown tag is English");
    assert_eq!(clarp_core::tr!("Follows this computer: {}", "English"), "Follows this computer: English");
    assert_eq!(clarp_core::tr!("{{literal}} {0} {1} {}", "a", "b"), "{literal} a b a");
}

#[test]
fn dates_and_times_follow_the_language() {
    let day = NaiveDate::from_ymd_opt(2026, 11, 26).unwrap();
    assert_eq!(format::weekday("nb", day), "torsdag");
    assert_eq!(format::weekday("zh-Hans", day), "星期四");
    assert_eq!(format::weekday("hi", day), "गुरुवार");
    assert_eq!(format::day_month("en", day), "Nov 26");
    assert_eq!(format::day_month("nb", day), "26. nov.");
    assert_eq!(format::day_month("zh-Hans", day), "11月26日");
    assert_eq!(format::date("fr", day), "26 nov. 2026");
    let at = NaiveTime::from_hms_opt(14, 5, 0).unwrap();
    assert_eq!(format::time("nb", at, format::Clock::Locale), "14:05");
    assert_eq!(format::time("en", at, format::Clock::H24), "14:05");
    assert!(format::time("en", at, format::Clock::Locale).starts_with("2:05"), "{}", format::time("en", at, format::Clock::Locale));
    assert_eq!(format::relative_day("nb", day, day.succ_opt().unwrap()).as_deref(), Some("I går"));
    assert_eq!(format::relative_day("en", day, day + chrono::Days::new(2)), None);
}

#[test]
fn numbers_and_relative_times_follow_the_language() {
    assert_eq!(format::number("en", 1_234_567), "1,234,567");
    assert_eq!(format::number("hi", 1_234_567), "12,34,567");
    assert_eq!(format::number("nb", 1_234_567), "1\u{a0}234\u{a0}567");
    assert_eq!(format::decimal("fr", 1234.5, 1), "1\u{202f}234,5");
    assert_eq!(format::relative("en", -10), "Just now");
    assert_eq!(format::relative("en", -300), "5 minutes ago");
    assert_eq!(format::relative("fr", -86_400), "il y a 1 jour");
    assert_eq!(format::relative("es", 2 * 3600), "dentro de 2 horas");
    assert_eq!(format::relative("zh-Hans", -3 * 86_400), "3 天前");
}

#[test]
fn lists_join_in_the_language() {
    assert_eq!(format::and_list::<&str>("en", &[]), "");
    assert_eq!(format::and_list("en", &["Arnold"]), "Arnold");
    assert_eq!(format::and_list("en", &["Arnold", "Hazel"]), "Arnold and Hazel");
    assert_eq!(format::and_list("en", &["laptop", "studio", "elitebook"]), "laptop, studio and elitebook");
    assert_eq!(format::and_list("es", &["Arnold", "Iris"]), "Arnold e Iris");
    assert_eq!(format::and_list("es", &["Arnold", "Hazel"]), "Arnold y Hazel");
    assert_eq!(format::and_list("nb", &["Arnold", "Hazel"]), "Arnold og Hazel");
    assert_eq!(format::and_list("fr", &["A", "B", "C"]), "A, B et C");
    assert_eq!(format::and_list("zh-Hans", &["A", "B", "C"]), "A、B和C");
}
