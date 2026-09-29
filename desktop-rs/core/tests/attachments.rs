use clarp_core::attachments::*;
use serde_json::json;

#[test]
fn content_type_sniffs_before_trusting_the_extension() {
    assert_eq!(content_type("photo.txt", b"\x89PNG\r\n\x1a\nrest"), "image/png");
    assert_eq!(content_type("scan.bin", b"%PDF-1.7"), "application/pdf");
    assert_eq!(content_type("notes.md", b"# hi"), "text/markdown");
    assert_eq!(content_type("blob", b"\x00\x01"), "application/octet-stream");
}

#[test]
fn sending_waits_for_uploads_and_appends_paths() {
    let ready = json!({"id": "a", "path": "/data/up/x.png", "status": "ready"});
    let local = json!({"id": "b", "path": "/home/u/y.pdf"});
    let uploading = json!({"id": "c", "status": "uploading"});
    assert!(can_send(&[ready.clone(), local.clone()]));
    assert!(!can_send(&[ready.clone(), uploading.clone()]));
    assert_eq!(outbound_text("  look at these  ", &[ready.clone(), local]).as_deref(),
               Some("look at these /data/up/x.png /home/u/y.pdf"));
    assert_eq!(outbound_text("", &[ready]).as_deref(), Some("/data/up/x.png"));
    assert_eq!(outbound_text("hi", &[uploading]), None);
    assert_eq!(outbound_text("hi", &[]).as_deref(), Some("hi"));
}
