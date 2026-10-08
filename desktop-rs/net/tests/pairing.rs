//! `clarp-slint --pair`'s code exchange against a scripted local Host.

use clarp_net::pairing;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use url::Url;

/// Answers one request with `status` and `body`; hands back the request.
async fn host_once(status: &'static str, body: &'static str) -> (Url, tokio::task::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = Url::parse(&format!("http://{}/clarp", listener.local_addr().unwrap())).unwrap();
    let served = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = Vec::new();
        let mut chunk = [0u8; 4096];
        loop {
            let n = socket.read(&mut chunk).await.unwrap();
            request.extend_from_slice(&chunk[..n]);
            let text = String::from_utf8_lossy(&request);
            if let Some((head, rest)) = text.split_once("\r\n\r\n") {
                let length = head.lines().find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().unwrap())).unwrap_or(0);
                if rest.len() >= length || n == 0 {
                    break;
                }
            }
        }
        let reply = format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        socket.write_all(reply.as_bytes()).await.unwrap();
        String::from_utf8_lossy(&request).into_owned()
    });
    (url, served)
}

#[tokio::test]
async fn a_good_code_brings_the_device_token() {
    let (url, served) = host_once("200 OK", r#"{"device":{"id":"d1","token":"cld_paired"}}"#).await;
    assert_eq!(pairing::exchange(&url, " 123456 ", "Clarp desktop on mac").await, Ok("cld_paired".into()));
    let request = served.await.unwrap();
    assert!(request.starts_with("POST /clarp/pairing/exchange "), "beneath the base path: {request}");
    assert!(request.contains(r#""code":"123456""#), "the code, trimmed: {request}");
    assert!(request.contains(r#""device_name":"Clarp desktop on mac""#), "{request}");
    assert!(!request.to_ascii_lowercase().contains("authorization:"), "the code is the only credential: {request}");
}

#[tokio::test]
async fn the_hosts_refusal_is_the_error() {
    let (url, _) = host_once("403 Forbidden", r#"{"error":"pairing code expired"}"#).await;
    assert_eq!(pairing::exchange(&url, "999999", "d").await, Err("pairing code expired".into()));
}

#[tokio::test]
async fn a_reply_without_a_token_fails() {
    let (url, _) = host_once("200 OK", r#"{"device":{"id":"d1"}}"#).await;
    assert_eq!(pairing::exchange(&url, "000000", "d").await, Err("Pairing response did not contain a device credential".into()));
}

#[tokio::test]
async fn an_unreachable_host_names_the_url() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = Url::parse(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
    drop(listener);
    let error = pairing::exchange(&url, "123456", "d").await.unwrap_err();
    assert!(error.starts_with(&format!("Could not reach {url}pairing/exchange")), "{error}");
}
