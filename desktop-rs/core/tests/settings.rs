use clarp_core::settings::*;

#[test]
fn base_urls_normalise_like_the_cpp_client() {
    assert_eq!(normalized_base_url("  https://host:7699//  "), "https://host:7699");
    assert_eq!(normalized_base_url(""), "http://127.0.0.1:7682");
}

#[test]
fn the_config_token_is_read_from_auth_token() {
    assert_eq!(token_from_config("name = \"x\"\n  auth_token = \"cld_123\"\n"), "cld_123");
    assert_eq!(token_from_config("# auth_token = \"no\"\n"), "");
}

#[test]
fn settings_survive_a_reload() {
    let dir = std::env::temp_dir().join(format!("clarp-settings-{}", std::process::id()));
    let path = dir.join("settings.json");
    let mut settings = Settings::at(&path);
    settings.set("conversation/showWhenReady", true);
    settings.set("connection/baseUrl", "http://h:1");
    let reloaded = Settings::at(&path);
    assert!(reloaded.boolean("conversation/showWhenReady", false));
    assert_eq!(reloaded.string("connection/baseUrl", ""), "http://h:1");
    assert_eq!(reloaded.integer("missing", 7), 7);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn draft_keys_are_scoped_to_host_and_session() {
    let a = draft_scope_key("http://h:1/", "rachel");
    assert_eq!(a, draft_scope_key("http://h:1", "rachel"), "trailing slash is not a different Host");
    assert_ne!(a, draft_scope_key("http://h:2", "rachel"));
    assert_ne!(a, draft_scope_key("http://h:1", "bella"));
    assert!(a.starts_with("composerDrafts/") && a.len() == "composerDrafts/".len() + 64);
}
