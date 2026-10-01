//! Composer attachment rules. Port of the attachment helpers in
//! `desktop/src/app/AppController.cpp`.

use serde_json::Value;

pub const MAX_UPLOAD_BYTES: u64 = 50 * 1024 * 1024;

/// MIME type from content first (like QMimeDatabase::MatchContent), then
/// the extension, else application/octet-stream.
pub fn content_type(file_name: &str, head: &[u8]) -> String {
    let sniffed = if head.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if head.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some("image/jpeg")
    } else if head.starts_with(b"GIF87a") || head.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if head.len() >= 12 && &head[..4] == b"RIFF" && &head[8..12] == b"WEBP" {
        Some("image/webp")
    } else if head.starts_with(b"%PDF-") {
        Some("application/pdf")
    } else if head.starts_with(b"PK\x03\x04") {
        Some("application/zip")
    } else {
        None
    };
    if let Some(sniffed) = sniffed {
        return sniffed.into();
    }
    let extension = file_name.rsplit_once('.').map(|(_, e)| e.to_ascii_lowercase()).unwrap_or_default();
    match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "svg" => "image/svg+xml",
        "pdf" => "application/pdf",
        "txt" | "log" => "text/plain",
        "md" => "text/markdown",
        "json" => "application/json",
        "csv" => "text/csv",
        "html" | "htm" => "text/html",
        "zip" => "application/zip",
        "mp3" => "audio/mpeg",
        "wav" => "audio/x-wav",
        "mp4" => "video/mp4",
        _ => "application/octet-stream",
    }
    .into()
}

fn ready(attachment: &Value) -> bool {
    let path = attachment.get("path").and_then(Value::as_str).unwrap_or_default();
    let status = attachment.get("status").and_then(Value::as_str).unwrap_or("ready");
    !path.is_empty() && status == "ready"
}

/// Every attachment has a path and finished uploading.
pub fn can_send(attachments: &[Value]) -> bool {
    attachments.iter().all(ready)
}

/// The outbound text: the typed text followed by each attachment path, or
/// None while an attachment is still uploading or failed.
pub fn outbound_text(text: &str, attachments: &[Value]) -> Option<String> {
    let mut outbound = text.trim().to_owned();
    for attachment in attachments {
        if !ready(attachment) {
            return None;
        }
        let path = attachment.get("path").and_then(Value::as_str).unwrap_or_default();
        if !outbound.is_empty() && !outbound.ends_with(' ') {
            outbound.push(' ');
        }
        outbound += path;
    }
    Some(outbound)
}
