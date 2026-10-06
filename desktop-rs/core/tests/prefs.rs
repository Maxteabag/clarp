use clarp_core::prefs::{self, Kind, Scope};
use clarp_core::settings::Settings;
use serde_json::{Value, json};

fn scratch(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("clarp-prefs-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir.join("settings.json")
}

#[test]
fn every_setting_has_a_label_description_aliases_section_and_a_valid_default() {
    let mut names = std::collections::HashSet::new();
    for spec in prefs::specs() {
        assert!(names.insert(spec.name), "{} is named twice", spec.name);
        assert!(!spec.label.is_empty(), "{} has a label", spec.name);
        assert!(spec.description.len() > 15, "{} says what it does: {:?}", spec.name, spec.description);
        assert!(spec.aliases.len() >= 2, "{} has at least two aliases", spec.name);
        assert!(prefs::SECTIONS.contains(&spec.section), "{} is in a known section", spec.name);
        for theme in clarp_core::reading_theme::themes() {
            let theme = theme["id"].as_str().unwrap();
            let default = prefs::default_value(spec, theme);
            assert!(prefs::validate(spec, &default).is_ok(), "{}'s default {default} is valid in {theme}", spec.name);
        }
        assert_eq!(prefs::find(spec.name).map(|s| s.name), Some(spec.name), "{} is found by its name", spec.name);
        assert_eq!(prefs::find(spec.label).map(|s| s.name), Some(spec.name), "{} is found by its label", spec.name);
    }
    assert!(prefs::specs().len() >= 50, "every knob is a setting ({})", prefs::specs().len());
}

#[test]
fn settings_are_found_by_the_words_people_use() {
    for (words, name) in [
        ("font-size", "fontsize"),
        ("Font size", "fontsize"),
        ("text size", "fontsize"),
        ("line spacing", "paragraphspacing"),
        ("appearance/uiScale", "uiscale"),
        ("activity bar", "navrail"),
        ("sidebar width", "explorerwidth"),
        ("24 hour", "timeformat"),
    ] {
        assert_eq!(prefs::find(words).map(|s| s.name), Some(name), "{words}");
    }
    assert!(prefs::find("no such thing").is_none());
}

#[test]
fn every_colour_role_the_window_draws_is_a_setting() {
    let roles: Vec<&str> = prefs::specs()
        .iter()
        .filter_map(|s| match s.kind {
            Kind::Color { role } => Some(role),
            _ => None,
        })
        .collect();
    for role in ["window", "raised", "sunken", "control", "hover", "border", "rule", "text", "chromeText", "mutedText", "faintText", "accent", "focus", "accentText", "bubble", "success", "warning", "danger", "dangerSurface", "link"] {
        assert!(roles.contains(&role), "{role} can be overridden");
    }
}

#[test]
fn a_change_is_saved_and_survives_a_restart_and_reset_brings_the_default_back() {
    let path = scratch("persist");
    let mut settings = Settings::at(&path);
    prefs::set_text(&mut settings, "fontsize", "18").unwrap();
    prefs::set_text(&mut settings, "radius", "0").unwrap();
    prefs::set_text(&mut settings, "timeformat", "24h").unwrap();
    let mut again = Settings::at(&path);
    assert_eq!(prefs::number_of(&again, "fontsize"), 18.0);
    assert_eq!(prefs::number_of(&again, "radius"), 0.0);
    assert_eq!(prefs::choice_of(&again, "timeformat"), "24h");
    assert_eq!(again.get("appearance/fontOverrides"), Some(&json!({"terminal": {"size": 18.0}})), "the size is the theme's own, beside the font picker's family");

    let theme = prefs::theme_of(&again);
    prefs::reset(&mut again, prefs::find("fontsize").unwrap(), &theme);
    assert_eq!(prefs::number_of(&again, "fontsize"), 15.0, "reset: the theme's size");
    assert!(again.get("appearance/fontOverrides").is_none(), "an empty override leaves no trace");
    assert_eq!(prefs::reset_section(&mut again, prefs::LAYOUT, &theme), 1, "reset section: the radius");
    assert_eq!(prefs::number_of(&again, "radius"), 6.0);
    assert_eq!(prefs::reset_all(&mut again, &theme), 1, "reset all: the clock");
    assert_eq!(prefs::choice_of(&Settings::at(&path), "timeformat"), "12h", "and the file forgot it");
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn setting_the_default_keeps_the_file_to_what_the_user_changed() {
    let mut settings = Settings::in_memory();
    prefs::set_text(&mut settings, "messagespacing", "20").unwrap();
    assert!(settings.get("layout/messageSpacing").is_some());
    prefs::set_text(&mut settings, "messagespacing", "14").unwrap();
    assert!(settings.get("layout/messageSpacing").is_none());
}

#[test]
fn plus_and_minus_step_numbers_within_their_range() {
    let mut settings = Settings::in_memory();
    let theme = prefs::theme_of(&settings);
    let size = prefs::find("fontsize").unwrap();
    assert_eq!(prefs::adjust(&mut settings, size, &theme, 1), Some(json!(16.0)));
    assert_eq!(prefs::adjust(&mut settings, size, &theme, -3), Some(json!(13.0)));
    for _ in 0..60 {
        prefs::adjust(&mut settings, size, &theme, 1);
    }
    assert_eq!(prefs::number_of(&settings, "fontsize"), 40.0, "clamped at the top");
    let scale = prefs::find("uiscale").unwrap();
    assert_eq!(prefs::adjust(&mut settings, scale, &theme, 1), Some(json!(1.2)), "on the step grid, no float noise");
    let measure = prefs::find("measure").unwrap();
    assert_eq!(prefs::adjust(&mut settings, measure, &theme, 1), Some(json!(40.0)), "from full width to the narrowest");
    assert_eq!(prefs::adjust(&mut settings, measure, &theme, -1), Some(json!(0.0)), "and back to full width");
    let density = prefs::find("density").unwrap();
    assert_eq!(prefs::adjust(&mut settings, density, &theme, 1), Some(json!("spacious")));
    assert_eq!(prefs::adjust(&mut settings, density, &theme, 1), Some(json!("compact")), "choices wrap");
    let timestamps = prefs::find("timestamps").unwrap();
    assert_eq!(prefs::adjust(&mut settings, timestamps, &theme, 1), Some(json!(true)));
}

#[test]
fn set_reads_what_vim_set_commands_say() {
    let mut settings = Settings::in_memory();
    for line in ["fontsize=17", "timestamps", "nonavrail", "fontsize+=2", "chromesize-=10", "timeformat=24-hour (18:10)", "color.text=#fff", "codefont: Iosevka"] {
        let (name, value) = prefs::split_assignment(line);
        prefs::set_text(&mut settings, &name, &value).unwrap_or_else(|e| panic!("{line}: {e}"));
    }
    assert_eq!(prefs::number_of(&settings, "fontsize"), 19.0);
    assert!(prefs::flag(&settings, "timestamps"));
    assert!(!prefs::flag(&settings, "navrail"));
    assert_eq!(prefs::number_of(&settings, "chromesize"), 90.0);
    assert_eq!(prefs::choice_of(&settings, "timeformat"), "24h", "a choice by its label");
    assert_eq!(prefs::shown(&settings, prefs::find("color.text").unwrap()), "#ffffff");
    assert_eq!(prefs::shown(&settings, prefs::find("codefont").unwrap()), "Iosevka");
    for bad in [("fontsize", "huge"), ("fontsize", "99"), ("timestamps", "maybe"), ("density", "dense"), ("color.text", "red"), ("nosuch", "1")] {
        assert!(prefs::set_text(&mut settings, bad.0, bad.1).is_err(), "{bad:?} is refused");
    }
    assert_eq!(prefs::number_of(&settings, "fontsize"), 19.0, "a refused value changes nothing");
}

#[test]
fn spoken_replies_and_old_integer_choices_keep_their_stored_form() {
    let mut settings = Settings::in_memory();
    prefs::set_text(&mut settings, "spokenreplies", "off").unwrap();
    assert_eq!(settings.get("audio/muted"), Some(&json!(true)), "the engine's own key");
    prefs::set_text(&mut settings, "toolactivity", "visible").unwrap();
    assert_eq!(settings.get("conversation/activityDisplayMode"), Some(&json!(1)));
    settings.set("experiments/toolDetailLevel", 3);
    assert_eq!(prefs::choice_of(&settings, "tooldetail"), "explained");
}

#[test]
fn invalid_values_in_the_file_are_reported_and_read_as_defaults() {
    let path = scratch("invalid");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(
        &path,
        r##"{"typography/measure": 5, "layout/radius": "big", "conversation/timestampsVisible": "yes",
            "appearance/colorOverrides": {"terminal": {"text": "nope", "glow": "#fff"}, "nosuch": {}},
            "appearance/fontOverrides": {"terminal": {"size": 400, "family": "Iosevka"}}}"##,
    )
    .unwrap();
    let settings = Settings::at(&path);
    let problems = prefs::problems(&settings);
    for word in ["measure", "radius", "timestamps", "color.text", "glow", "nosuch", "fontsize"] {
        assert!(problems.iter().any(|p| p.contains(word)), "{word} is reported: {problems:#?}");
    }
    assert_eq!(prefs::number_of(&settings, "measure"), 0.0);
    assert_eq!(prefs::number_of(&settings, "radius"), 6.0);
    assert_eq!(prefs::number_of(&settings, "fontsize"), 15.0);
    assert!(!prefs::flag(&settings, "timestamps"));
    assert_eq!(prefs::shown(&settings, prefs::find("color.text").unwrap()), "#e7e1dc");
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn an_edited_file_reloads_and_a_broken_one_keeps_the_settings() {
    let path = scratch("reload");
    let mut settings = Settings::at(&path);
    prefs::set_text(&mut settings, "radius", "3").unwrap();
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(text.contains("\n  \"layout/radius\""), "saved pretty, to edit by hand: {text}");
    std::fs::write(&path, text.replace("3.0", "9")).unwrap();
    assert_eq!(settings.reload().unwrap(), vec!["layout/radius".to_owned()]);
    assert_eq!(prefs::number_of(&settings, "radius"), 9.0);
    std::fs::write(&path, "{ \"layout/radius\": ").unwrap();
    assert!(settings.reload().is_err(), "half-written JSON is an error");
    assert_eq!(prefs::number_of(&settings, "radius"), 9.0, "and changes nothing");
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[test]
fn export_and_import_round_trip_and_import_skips_what_is_wrong() {
    let mut settings = Settings::in_memory();
    settings.set("keymap/bindings", json!({"x": 1}));
    prefs::set_text(&mut settings, "fontsize", "17").unwrap();
    prefs::set_text(&mut settings, "density", "compact").unwrap();
    prefs::set_color(&mut settings, "accent", "#ff8800").unwrap();
    let exported = prefs::export(&settings);
    assert!(exported.get("keymap/bindings").is_none(), "only preferences, not the window's state");
    let mut other = Settings::in_memory();
    let (applied, problems) = prefs::import(&mut other, &exported);
    assert!(problems.is_empty(), "{problems:?}");
    assert_eq!(applied, 3);
    assert_eq!(prefs::number_of(&other, "fontsize"), 17.0);
    assert_eq!(prefs::choice_of(&other, "density"), "compact");
    assert_eq!(prefs::shown(&other, prefs::find("color.accent").unwrap()), "#ff8800");

    let (applied, problems) = prefs::import(&mut other, &json!({"layout/radius": 2, "layout/paneGap": -4, "bogus": 1, "appearance/colorOverrides": {"terminal": {"link": "blue"}}}));
    assert_eq!(applied, 1);
    assert_eq!(problems.len(), 3, "{problems:?}");
    assert_eq!(prefs::number_of(&other, "radius"), 2.0);
    assert_eq!(prefs::shown(&other, prefs::find("color.accent").unwrap()), "#ff8800", "what the file leaves out is kept");
    assert!(prefs::import(&mut other, &json!([1, 2])).1.len() == 1);
}

#[test]
fn a_colour_override_below_the_readability_rules_warns_but_applies() {
    let mut settings = Settings::in_memory();
    let window = prefs::shown(&settings, prefs::find("color.window").unwrap());
    let warnings = prefs::set_color(&mut settings, "text", &window).unwrap();
    assert!(warnings.iter().any(|w| w.contains("text on window")), "{warnings:?}");
    assert_eq!(prefs::shown(&settings, prefs::find("color.text").unwrap()), window, "applied anyway");
    assert!(prefs::set_color(&mut settings, "text", "#ffffff").unwrap().is_empty(), "white on the dark window is fine");
    assert!(prefs::set_color(&mut settings, "text", "not a colour").is_err());
    assert!(prefs::palette("terminal").iter().any(|(role, _)| role == "accent"), "the picker offers the theme's palette");
}

#[test]
fn the_schema_and_reference_document_every_setting() {
    let schema = prefs::schema();
    let properties = schema["properties"].as_object().unwrap();
    let reference = prefs::reference();
    for spec in prefs::specs() {
        if spec.scope == Scope::Global {
            let property = &properties[spec.key];
            assert!(property["description"].as_str().unwrap().contains(spec.description), "{} is described in the schema", spec.name);
        }
        assert!(reference.contains(&format!("`{}`", spec.name)), "{} is in the reference", spec.name);
    }
    assert!(properties.contains_key("appearance/colorOverrides") && properties.contains_key("appearance/fontOverrides"));
    assert_eq!(schema["properties"]["layout/radius"]["maximum"], Value::from(20.0));
}
