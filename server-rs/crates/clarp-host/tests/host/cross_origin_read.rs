use super::*;

const ADMIN: &str = "fixture-administrator";
const WEB: &str = "https://studio.example";
const LOOPBACK: &str = "http://localhost:5173";

fn start(place: &Place, token: &str, file: &str) -> Host {
    let xdg = place.root.join("xdg");
    std::fs::create_dir_all(xdg.join("clarp-host")).unwrap();
    std::fs::write(xdg.join("clarp-host/config.toml"), file).unwrap();
    place.start_in(token, &[], &[("XDG_CONFIG_HOME", xdg.to_str().unwrap())])
}

fn request(
    host: &Host,
    method: &str,
    path: &str,
    token: &str,
    headers: &[(&str, &str)],
) -> Fetched {
    fetch(host, token, method, path, headers, b"")
}

fn no_cors(answer: &Fetched) {
    assert!(
        answer
            .headers
            .iter()
            .all(|(key, _)| !key.starts_with("access-control-"))
    );
}

fn cors(answer: &Fetched, origin: &str) {
    assert_eq!(answer.header("access-control-allow-origin"), origin);
    assert!(answer.header("vary").eq_ignore_ascii_case("origin"));
    for name in [
        "access-control-allow-credentials",
        "access-control-expose-headers",
    ] {
        assert!(!answer.headers.iter().any(|(key, _)| key == name));
    }
}

#[test]
fn cross_origin_read_has_no_allowance_by_default() {
    let place = Place::new("cross-origin-default");
    let host = start(&place, ADMIN, "[server]\n");
    let (device, _) = pair(&host, "Web app", "full");
    let got = request(&host, "GET", "/server-info", &device, &[("Origin", WEB)]);
    assert_eq!(got.status, 200);
    no_cors(&got);
    let preflight = request(
        &host,
        "OPTIONS",
        "/server-info",
        "",
        &[("Origin", WEB), ("Access-Control-Request-Method", "GET")],
    );
    assert_eq!(preflight.status, 403);
    no_cors(&preflight);
}

#[test]
fn cross_origin_preflights_only_allow_the_two_get_routes_and_the_named_headers() {
    let place = Place::new("cross-origin-preflight");
    let host = start(
        &place,
        ADMIN,
        "[server]\nweb_read_origins = [\"HTTPS://Studio.Example:443/\", \"https://*.example\", \"null\", 9]",
    );
    for path in ["/server-info", "/agents/snapshot?include_archived=0"] {
        let allowed = request(
            &host,
            "OPTIONS",
            path,
            "",
            &[
                ("Origin", WEB),
                ("Access-Control-Request-Method", "GET"),
                (
                    "Access-Control-Request-Headers",
                    "AUTHORIZATION, x-clarp-client",
                ),
            ],
        );
        assert_eq!(allowed.status, 204);
        assert!(allowed.body.is_empty());
        cors(&allowed, WEB);
        assert_eq!(allowed.header("access-control-allow-methods"), "GET");
        assert_eq!(
            allowed.header("access-control-allow-headers"),
            "Authorization, X-Clarp-Client"
        );
        assert_eq!(allowed.header("access-control-max-age"), "600");
    }
    for (path, origin, method, headers) in [
        ("/log", WEB, "GET", "authorization"),
        ("/server-info", WEB, "HEAD", "authorization"),
        ("/server-info", WEB, "POST", "authorization"),
        ("/server-info", WEB, "GET", "authorization, content-type"),
        (
            "/server-info",
            "https://other.example",
            "GET",
            "authorization",
        ),
        ("/server-info", "null", "GET", "authorization"),
        (
            "/server-info",
            "https://studio.example:443",
            "GET",
            "authorization",
        ),
        (
            "/server-info",
            "https://studio.example/",
            "GET",
            "authorization",
        ),
        ("/%73erver-info", WEB, "GET", "authorization"),
    ] {
        let denied = request(
            &host,
            "OPTIONS",
            path,
            "",
            &[
                ("Origin", origin),
                ("Access-Control-Request-Method", method),
                ("Access-Control-Request-Headers", headers),
            ],
        );
        assert_eq!(denied.status, 403, "{path} {origin} {method} {headers}");
        no_cors(&denied);
    }
}

#[test]
fn cross_origin_reads_take_only_device_bearers_and_keep_device_scope() {
    let place = Place::new("cross-origin-device");
    let host = start(
        &place,
        ADMIN,
        "[server]\nweb_read_origins = [\"http://localhost:5173\"]",
    );
    let (device, _) = pair(&host, "Web app", "full");
    let (limited, _) = pair(&host, "Web reader", "limited");
    let page = [("Origin", LOOPBACK), ("Sec-Fetch-Site", "same-site")];
    for path in ["/server-info", "/agents/snapshot"] {
        let got = request(&host, "GET", path, &device, &page);
        assert_eq!(got.status, 200);
        cors(&got, LOOPBACK);
        serde_json::from_slice::<Value>(&got.body).unwrap();

        let got = request(&host, "GET", path, &limited, &page);
        assert_eq!(got.status, 200);
        cors(&got, LOOPBACK);
        let info: Value = serde_json::from_slice(&got.body).unwrap();
        if path == "/server-info" {
            assert!(info.get("default_cwd").is_none());
        }
    }
    let cookie = format!("claude_pwa_token={device}");
    let query = format!("/server-info?token={device}");
    for (path, token, extra) in [
        ("/server-info", "", Vec::new()),
        ("/server-info", ADMIN, Vec::new()),
        ("/agents/snapshot", ADMIN, Vec::new()),
        ("/server-info", "", vec![("Cookie", cookie.as_str())]),
        (query.as_str(), "", Vec::new()),
    ] {
        let headers = [page.as_slice(), &extra].concat();
        let denied = request(&host, "GET", path, token, &headers);
        assert_eq!(denied.status, 401);
        cors(&denied, LOOPBACK);
    }
    no_cors(&request(&host, "GET", "/artifacts", &device, &page));
    no_cors(&request(&host, "HEAD", "/server-info", &device, &page));
    no_cors(&request(
        &host,
        "GET",
        "/server-info",
        &device,
        &[("Origin", WEB)],
    ));
    let same_origin = request(&host, "GET", "/server-info", ADMIN, &[]);
    assert_eq!(same_origin.status, 200);
    no_cors(&same_origin);
}

#[test]
fn cross_origin_reads_do_not_inherit_the_local_no_token_exemption() {
    let place = Place::new("cross-origin-no-token");
    let host = start(
        &place,
        "",
        "[server]\nweb_read_origins = [\"http://localhost:5173\"]",
    );
    for path in ["/server-info", "/agents/snapshot"] {
        let denied = request(&host, "GET", path, "", &[("Origin", LOOPBACK)]);
        assert_eq!(denied.status, 401);
        cors(&denied, LOOPBACK);
        let local = request(&host, "GET", path, "", &[]);
        assert_eq!(local.status, 200);
        no_cors(&local);
    }
}

#[test]
fn cross_origin_headers_do_not_leak_to_the_next_request_on_the_same_socket() {
    let place = Place::new("cross-origin-keep-alive");
    let host = start(
        &place,
        ADMIN,
        "[server]\nweb_read_origins = [\"https://studio.example\"]",
    );
    let (device, _) = pair(&host, "Web app", "full");
    let mut socket = TcpStream::connect(&host.address).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut reader = BufReader::new(socket.try_clone().unwrap());
    for origin in [format!("Origin: {WEB}\r\n"), String::new()] {
        write!(socket, "GET /server-info HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {device}\r\n{origin}\r\n").unwrap();
        let mut status = String::new();
        reader.read_line(&mut status).unwrap();
        assert!(status.starts_with("HTTP/1.1 200"), "{status}");
        let mut head = Vec::new();
        loop {
            let mut line = String::new();
            assert!(reader.read_line(&mut line).unwrap() > 0);
            if line == "\r\n" {
                break;
            }
            let (key, value) = line.split_once(':').unwrap();
            head.push((key.to_ascii_lowercase(), value.trim().to_owned()));
        }
        let size = head
            .iter()
            .find(|(key, _)| key == "content-length")
            .unwrap()
            .1
            .parse()
            .unwrap();
        let mut body = vec![0; size];
        reader.read_exact(&mut body).unwrap();
        let answer = Fetched {
            status: 200,
            headers: head,
            body,
        };
        if origin.is_empty() {
            no_cors(&answer);
        } else {
            cors(&answer, WEB);
        }
    }
}
