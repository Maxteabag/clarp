//! A chat's own Tool activity (#653): kept on this device under its Host's
//! server id and its session, "Follow Settings" when it has none.

use clarp_core::presentation::{self, CHAT_ACTIVITY_KEY, GROUPED, HIDE_WHEN_DONE, chat_activity, chat_key, set_chat_activity};
use clarp_core::settings::Settings;

#[test]
fn a_chat_is_keyed_by_its_hosts_server_id_and_its_session() {
    assert_eq!(chat_key("srv-1", "rachel"), "@srv-1/rachel");
    assert_eq!(chat_key("srv-1", "@srv-2/mike"), "@srv-2/mike", "a peer chat is already addressed by its own Host");
    assert_eq!(chat_key("", "rachel"), "", "no key before the Host said its server id");
    assert_eq!(chat_key("srv-1", "rachel"), clarp_core::all_hosts::qualify("srv-1", "rachel"), "as the all-Hosts work addresses a chat");
}

#[test]
fn a_chats_own_choice_overrides_the_global_one_and_follow_settings_clears_it() {
    let mut settings = Settings::in_memory();
    let rachel = chat_key("srv-1", "rachel");
    let other_host = chat_key("srv-2", "rachel");
    assert_eq!(chat_activity(&settings, &rachel), None, "Follow Settings by default");
    set_chat_activity(&mut settings, &rachel, Some(HIDE_WHEN_DONE));
    assert_eq!(chat_activity(&settings, &rachel), Some(HIDE_WHEN_DONE));
    assert_eq!(chat_activity(&settings, &other_host), None, "the same session on another Host is another chat");
    set_chat_activity(&mut settings, &other_host, Some(GROUPED));
    set_chat_activity(&mut settings, &rachel, None);
    assert_eq!(chat_activity(&settings, &rachel), None, "Follow Settings again");
    assert_eq!(chat_activity(&settings, &other_host), Some(GROUPED));
    set_chat_activity(&mut settings, &other_host, None);
    assert!(settings.get(CHAT_ACTIVITY_KEY).is_none(), "nothing left stored");
    // A stored value out of range is not a choice.
    settings.set(CHAT_ACTIVITY_KEY, serde_json::json!({"@srv-1/rachel": 9}));
    assert_eq!(chat_activity(&settings, &rachel), None);
    assert_eq!(presentation::CHAT_ACTIVITY_CHOICES.map(|(_, label)| label), ["Follow Settings", "Grouped", "Always visible", "Group old", "Hide when done"]);
}
