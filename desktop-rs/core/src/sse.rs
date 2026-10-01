//! Incremental `text/event-stream` parser. Port of
//! `desktop/src/network/SseParser`: only blocks whose data is a non-empty JSON
//! object are delivered; comments, keep-alives and bad JSON are dropped.

use crate::json::Object;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SseMessage {
    pub id: String,
    pub event: String,
    pub data: Object,
}

#[derive(Debug, Default)]
pub struct SseParser {
    buffer: Vec<u8>,
}

impl SseParser {
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<SseMessage> {
        self.buffer.extend_from_slice(bytes);
        // A CRLF split across chunks is normalised once its LF arrives.
        let mut normalized = Vec::with_capacity(self.buffer.len());
        let mut i = 0;
        while i < self.buffer.len() {
            if self.buffer[i] == b'\r' && self.buffer.get(i + 1) == Some(&b'\n') {
                i += 1;
                continue;
            }
            normalized.push(self.buffer[i]);
            i += 1;
        }
        self.buffer = normalized;

        let mut messages = Vec::new();
        while let Some(boundary) = self.buffer.windows(2).position(|w| w == b"\n\n") {
            let block: Vec<u8> = self.buffer.drain(..boundary + 2).take(boundary).collect();
            let message = parse_block(&block);
            if !message.data.is_empty() {
                messages.push(message);
            }
        }
        messages
    }

    pub fn reset(&mut self) {
        self.buffer.clear();
    }
}

fn parse_block(block: &[u8]) -> SseMessage {
    let mut message = SseMessage::default();
    let mut data: Vec<u8> = Vec::new();
    for line in block.split(|b| *b == b'\n') {
        if line.is_empty() || line[0] == b':' {
            continue;
        }
        let (field, mut value) = match line.iter().position(|b| *b == b':') {
            Some(separator) => (&line[..separator], &line[separator + 1..]),
            None => (line, &[][..]),
        };
        if value.first() == Some(&b' ') {
            value = &value[1..];
        }
        match field {
            b"id" => message.id = String::from_utf8_lossy(value).into_owned(),
            b"event" => message.event = String::from_utf8_lossy(value).into_owned(),
            b"data" => {
                if !data.is_empty() {
                    data.push(b'\n');
                }
                data.extend_from_slice(value);
            }
            _ => {}
        }
    }
    if let Ok(serde_json::Value::Object(object)) = serde_json::from_slice(&data) {
        message.data = object;
    }
    message
}
