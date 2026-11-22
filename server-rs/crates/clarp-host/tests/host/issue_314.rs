use super::*;

#[test]
fn a_contact_in_use_cannot_be_deleted() {
    let place = Place::new("contact-in-use");
    let host = place.start();
    assert_eq!(
        host.ask("POST", "/personas", Some(&json!({"name": "Pilot"})))
            .0,
        201
    );
    let session = host.new_agent("Pilot");
    assert_eq!(
        host.ask("DELETE", "/personas/pilot", None),
        (409, json!({"error": "contact is in use by a chat"}))
    );
    host.post(
        "/agent-archive",
        json!({"session": session, "archived": true}),
    );
    assert_eq!(host.ask("DELETE", "/personas/Pilot", None).0, 409);
    assert_eq!(
        host.ask("POST", "/personas", Some(&json!({"name": "Spare"})))
            .0,
        201
    );
    assert_eq!(host.ask("DELETE", "/personas/Spare", None).0, 200);
}

#[test]
fn the_host_reports_its_android_client_contract_floor_to_a_limited_device() {
    let place = Place::new("android-contract");
    let host = place.start_with_token("the-administrators-token");
    let (device, _) = pair(&host, "Reader", "limited");
    let (status, info) = host.ask_as(&device, "GET", "/server-info", None);
    assert_eq!(status, 200);
    assert_eq!(info["contract"]["min_android"], 1);
    assert_eq!(info["contract"]["min_ios"], 1);
}
