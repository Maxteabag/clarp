use tauri::Manager;

fn server_value(key: &str) -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let text = std::fs::read_to_string(format!("{home}/.config/clarp/config.toml")).ok()?;
    let mut in_server = false;
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if line.starts_with('[') {
            in_server = line == "[server]";
            continue;
        }
        if !in_server {
            continue;
        }
        let mut parts = line.splitn(2, '=');
        if parts.next()?.trim() != key {
            continue;
        }
        let value = parts.next()?.trim().trim_matches('"').to_string();
        if value.is_empty() {
            return None;
        }
        return Some(value);
    }
    None
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let host = server_value("bind_addr").unwrap_or_else(|| "127.0.0.1".to_string());
            let port = server_value("port").unwrap_or_else(|| "7682".to_string());
            let mut url = format!("http://{host}:{port}/");
            if let Some(token) = server_value("auth_token") {
                url.push_str(&format!("?token={token}"));
            }
            if let Some(win) = app.get_webview_window("main") {
                let parsed = tauri::Url::parse(&url).map_err(|e| {
                    std::io::Error::new(std::io::ErrorKind::InvalidInput, e.to_string())
                })?;
                win.navigate(parsed)?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
