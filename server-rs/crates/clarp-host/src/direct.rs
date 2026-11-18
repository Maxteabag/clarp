//! Pushes handed to the push service itself, with a key of the Host's own.
//!
//! The relay carries what a phone is shown ([`crate::relay`]). It carries no push of the
//! kind a phone must show as an incoming call, which is how a ring rings a phone that is
//! locked or asleep. A Host whose owner gave it a key at the push service hands such a push
//! to the service itself: `[apns]` of the file the owner names for it (`--push-keys`) has
//! the key's file, its id, the team, and what is to be said of the app (`key_path`, `key_id`,
//! `team_id`, `bundle_id`, `environment`), as the Python Host reads them. Without that word,
//! or without the key, this Host pushes no call, and says so to whoever rings
//! (`delivery.voip.enabled` is false). The word is one of its own on purpose: a Host that is
//! only being tried reads its owner's keys of speech from his file, and must not push to his
//! phones with his key because that file names it too.
//!
//! The rules are the kernel's ([`clarp_kernel::direct_push`], the Python Host's
//! `server/lib/apns.py`): the request for one push, what an answer means, for how long a
//! token is used, when a push is handed over a second time. Here is what a Host adds: the
//! key is read and a token signed with it, and the service is spoken to over one connection
//! that is kept, as the service asks of those who push.
//!
//! The key never leaves the Host and nothing of it is said anywhere: what is said of a
//! failure never holds the key, a token signed with it, a phone's token, or what a push says.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bytes::Bytes;
use clarp_kernel::direct_push::{self as rules, Came, Next, Push, Signed, Signer, Tries};
use clarp_kernel::python;
use clarp_ports::clock::Clock;
use h2::client::SendRequest;
use serde_json::Value;
use tokio::net::TcpStream;

use crate::owner::{OwnerFile, text_at};

/// How long one push may take, from the asking to the answer.
const PUSH_WITHIN: Duration = Duration::from_secs(10);
/// For how long a connection that is not used is kept. The pushes of one ring go over one
/// connection; a ring an hour later makes its own, since a connection left alone for long
/// may be gone with nobody having said so, and a push sent into it would be lost.
const KEPT_FOR: Duration = Duration::from_secs(30);
/// How much of an answer of the service is read: it answers in a line.
const ANSWER_MOST: usize = 16 * 1024;
/// The app the owner's phones run, where his file names no other.
const BUNDLE: &str = "com.maxteabag.clarp";

/// What the owner gave this Host to push with.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Key {
    pub file: PathBuf,
    pub key_id: String,
    pub team_id: String,
    /// The app the phones' tokens belong to.
    pub bundle_id: String,
    /// The side of the push service a phone's token belongs to where the phone did not say.
    pub environment: String,
}

/// The key the owner's file names, when it names one whole and the file is there to read.
pub fn key(owner: &OwnerFile) -> Option<Key> {
    let file = owner.read()?;
    let named = text_at(&file, &["apns", "key_path"])?;
    let path = match named.strip_prefix("~/") {
        Some(rest) => crate::config::home().join(rest),
        None => PathBuf::from(&named),
    };
    let key = Key {
        key_id: text_at(&file, &["apns", "key_id"])?,
        team_id: text_at(&file, &["apns", "team_id"])?,
        bundle_id: text_at(&file, &["apns", "bundle_id"]).unwrap_or_else(|| BUNDLE.to_string()),
        environment: text_at(&file, &["apns", "environment"])
            .map_or_else(|| "production".to_string(), |said| said.to_lowercase()),
        file: path,
    };
    // A key that cannot be read is no key: nothing is tried with it.
    let readable = key.file.is_file() && std::fs::File::open(&key.file).is_ok();
    readable.then_some(key)
}

/// Why a push could not be handed over, in words that hold nothing of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsent(pub String);

/// The bytes of a key as its file has them: what stands between the two lines that say
/// where a private key begins and ends.
fn of_pem(text: &str) -> Option<Vec<u8>> {
    let mut lines = text.lines().map(str::trim);
    lines.find(|line| *line == "-----BEGIN PRIVATE KEY-----")?;
    let mut body = String::new();
    for line in lines {
        if line == "-----END PRIVATE KEY-----" {
            return python::b64decode(&body);
        }
        body.push_str(line);
    }
    None
}

/// Signs a token with the key, for this second.
fn signed(key: &Key, now_s: i64) -> Result<String, Unsent> {
    use ring::signature::{ECDSA_P256_SHA256_FIXED_SIGNING, EcdsaKeyPair};
    let unusable = |why: &str| Unsent(format!("the key at the push service is not usable: {why}"));
    let text = std::fs::read_to_string(&key.file).map_err(|_| unusable("its file is not read"))?;
    let bytes = of_pem(&text).ok_or_else(|| unusable("its file holds no private key"))?;
    let random = ring::rand::SystemRandom::new();
    let pair = EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_FIXED_SIGNING, &bytes, &random)
        .map_err(|_| unusable("it is no key of the kind the service takes"))?;
    let (head, claims) = rules::token(&key.team_id, &key.key_id, now_s);
    let said = rules::to_sign(&head, &claims);
    let signature = pair
        .sign(&random, said.as_bytes())
        .map_err(|_| unusable("nothing could be signed with it"))?;
    Ok(format!("{said}.{}", rules::base64url(signature.as_ref())))
}

/// Who signs: the key as it is now.
fn signer(key: &Key) -> Signer {
    let file = key.file.canonicalize().unwrap_or_else(|_| key.file.clone());
    let written = std::fs::metadata(&file)
        .and_then(|file| file.modified())
        .ok();
    let since = written.and_then(|at| at.duration_since(std::time::UNIX_EPOCH).ok());
    Signer {
        team_id: key.team_id.clone(),
        key_id: key.key_id.clone(),
        key_file: file.display().to_string(),
        written_ns: since.map_or(0, |since| i128::try_from(since.as_nanos()).unwrap_or(0)),
    }
}

/// The connection to the push service: which side of it, and when it was last used.
type Kept = Option<(String, SendRequest<Bytes>, tokio::time::Instant)>;

/// The way to the push service.
pub struct Direct {
    owner: OwnerFile,
    /// A stand-in for the service on this machine, for a test; empty: the service itself.
    stand_in: String,
    clock: Arc<dyn Clock + Send + Sync>,
    token: Mutex<Option<Signed>>,
    kept: tokio::sync::Mutex<Kept>,
}

/// What one try came to: the service's answer, or how it failed.
enum Tried {
    Answered(u16, String, String),
    /// Nothing went out: the connection was gone before.
    Closed,
    Failed(String),
}

impl Direct {
    pub fn new(owner: OwnerFile, stand_in: &str, clock: Arc<dyn Clock + Send + Sync>) -> Self {
        Self {
            owner,
            stand_in: stand_in.trim_end_matches('/').to_string(),
            clock,
            token: Mutex::new(None),
            kept: tokio::sync::Mutex::new(None),
        }
    }

    /// The key the owner gave, as his file names it now.
    pub fn key(&self) -> Option<Key> {
        key(&self.owner)
    }

    /// The token to push with now: the one in use while it serves, else one signed anew.
    fn token(&self, key: &Key, anew: bool) -> Result<String, Unsent> {
        let now_s = self.clock.now_ms().div_euclid(1000);
        let by = signer(key);
        let mut held = self
            .token
            .lock()
            .map_err(|_| Unsent("the token in use is not to be had".to_string()))?;
        if let Some(signed) = held.as_ref().filter(|_| !anew)
            && signed.serves(&by, now_s)
        {
            return Ok(signed.token.clone());
        }
        let token = signed(key, now_s)?;
        *held = Some(Signed {
            token: token.clone(),
            at_s: now_s,
            by,
        });
        Ok(token)
    }

    /// The connection to one side of the service: the one that is kept, or a new one.
    async fn line(&self, base: &str, anew: bool) -> Result<SendRequest<Bytes>, String> {
        let mut kept = self.kept.lock().await;
        let now = tokio::time::Instant::now();
        if let Some((to, line, used)) = kept.as_mut().filter(|_| !anew)
            && to == base
            && now.duration_since(*used) < KEPT_FOR
        {
            *used = now;
            return Ok(line.clone());
        }
        let line = connect(base).await?;
        *kept = Some((base.to_string(), line.clone(), now));
        Ok(line)
    }

    /// One push to one phone, handed over at most twice. Answers the service's status, its
    /// reason and the name it gave the push.
    pub async fn push(
        &self,
        key: &Key,
        (device_token, environment): (&str, &str),
        push: &Push<'_>,
    ) -> Result<(u16, Value, String), Unsent> {
        let environment = match environment {
            "" => key.environment.as_str(),
            said => said,
        };
        let base = match self.stand_in.as_str() {
            "" => rules::host(environment),
            stand_in => stand_in,
        };
        let mut tries = Tries::default();
        let (mut new_token, mut new_line) = (false, false);
        loop {
            let token = self.token(key, new_token)?;
            new_token = false;
            let request = rules::request(&token, &key.bundle_id, device_token, push);
            let tried = match self.line(base, new_line).await {
                Ok(line) => {
                    let asked = ask(line, base, request);
                    match tokio::time::timeout(PUSH_WITHIN, asked).await {
                        Ok(tried) => tried,
                        Err(_) => Tried::Failed("the service did not answer in time".to_string()),
                    }
                }
                // No connection at all is no connection somebody closed: nothing is tried
                // again for it.
                Err(why) => Tried::Failed(why),
            };
            new_line = false;
            let (came, answer) = match tried {
                Tried::Answered(status, body, named) => {
                    let answer = rules::answered(status, &body, &named);
                    (Came::Answered(status), Ok(answer))
                }
                Tried::Closed => (
                    Came::Closed,
                    Err(Unsent(
                        "the connection to the push service was closed".to_string(),
                    )),
                ),
                Tried::Failed(why) => (Came::Failed, Err(Unsent(why))),
            };
            if answer.is_err() {
                // Whatever failed, the connection is not kept: the next push makes its own.
                *self.kept.lock().await = None;
            }
            let reason = answer
                .as_ref()
                .map_or("", |(_, reason, _)| reason.as_str().unwrap_or(""));
            match tries.after(came, reason) {
                Next::Done => return answer,
                Next::AgainOverANewConnection => new_line = true,
                Next::AgainWithANewToken => new_token = true,
            }
        }
    }
}

/// The words for a service that was not reached, whatever kept it away.
fn not_reached<E>(_: E) -> String {
    "the push service was not reached".to_string()
}

/// Opens a connection to the service: over TLS to the service itself, as plain HTTP/2 to a
/// stand-in on this machine.
async fn connect(base: &str) -> Result<SendRequest<Bytes>, String> {
    let (secure, rest) = match base.strip_prefix("https://") {
        Some(rest) => (true, rest),
        None => (false, base.strip_prefix("http://").unwrap_or(base)),
    };
    let (host, address) = match rest.rsplit_once(':') {
        Some((host, port))
            if port.bytes().all(|sign| sign.is_ascii_digit()) && !port.is_empty() =>
        {
            (host.to_string(), rest.to_string())
        }
        _ => (rest.to_string(), format!("{rest}:443")),
    };
    let stream = TcpStream::connect(&address).await.map_err(not_reached)?;
    let _ = stream.set_nodelay(true);
    let line = if secure {
        let mut roots = rustls::RootCertStore::empty();
        roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let mut trust = rustls::ClientConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .map_err(|_| "no way to speak TLS".to_string())?
            .with_root_certificates(roots)
            .with_no_client_auth();
        // The service speaks HTTP/2 and nothing else.
        trust.alpn_protocols = vec![b"h2".to_vec()];
        let name = rustls::pki_types::ServerName::try_from(host)
            .map_err(|_| "the push service has no name to be known by".to_string())?;
        let secured = tokio_rustls::TlsConnector::from(Arc::new(trust));
        let stream = secured.connect(name, stream).await.map_err(not_reached)?;
        let (line, connection) = h2::client::handshake(stream).await.map_err(not_reached)?;
        tokio::spawn(async move {
            let _ = connection.await;
        });
        line
    } else {
        let (line, connection) = h2::client::handshake(stream).await.map_err(not_reached)?;
        tokio::spawn(async move {
            let _ = connection.await;
        });
        line
    };
    Ok(line)
}

/// Hands one request to the service over a connection, and reads its answer.
async fn ask(line: SendRequest<Bytes>, base: &str, request: rules::Request) -> Tried {
    // Until the request is on its way nothing reached the service.
    let Ok(mut line) = line.ready().await else {
        return Tried::Closed;
    };
    let mut asked = http::Request::builder()
        .method("POST")
        .uri(format!("{base}{}", request.path));
    for (name, value) in &request.headers {
        asked = asked.header(*name, value);
    }
    let Ok(asked) = asked.body(()) else {
        return Tried::Failed("the request could not be made".to_string());
    };
    let Ok((answer, mut body)) = line.send_request(asked, false) else {
        return Tried::Closed;
    };
    let failed = || Tried::Failed("the push service did not take the push".to_string());
    if body.send_data(Bytes::from(request.body), true).is_err() {
        return failed();
    }
    // Once the request is on its way nobody here knows whether it reached the service: it is
    // not handed over a second time for a failure after that.
    let Ok(answer) = answer.await else {
        return failed();
    };
    let status = answer.status().as_u16();
    let named = answer.headers().get("apns-id");
    let named = named.and_then(|value| value.to_str().ok()).unwrap_or("");
    let named = named.to_string();
    let mut said = Vec::new();
    let mut rest = answer.into_body();
    while let Some(piece) = rest.data().await {
        let Ok(piece) = piece else {
            return failed();
        };
        let _ = rest.flow_control().release_capacity(piece.len());
        if said.len() < ANSWER_MOST {
            said.extend_from_slice(&piece);
        }
    }
    Tried::Answered(status, String::from_utf8_lossy(&said).into_owned(), named)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// A key of a test's own in a file, as the push service's are written.
    fn key_file(name: &str) -> (PathBuf, Vec<u8>) {
        use ring::signature::{ECDSA_P256_SHA256_FIXED_SIGNING, EcdsaKeyPair, KeyPair};
        let random = ring::rand::SystemRandom::new();
        let made = EcdsaKeyPair::generate_pkcs8(&ECDSA_P256_SHA256_FIXED_SIGNING, &random).unwrap();
        let pair =
            EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_FIXED_SIGNING, made.as_ref(), &random)
                .unwrap();
        let body = python::b64encode(made.as_ref());
        let lines: Vec<&str> = (0..body.len().div_ceil(64))
            .map(|line| &body[line * 64..body.len().min(line * 64 + 64)])
            .collect();
        let text = format!(
            "-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----\n",
            lines.join("\n")
        );
        let file =
            std::env::temp_dir().join(format!("clarp-direct-{name}-{}.p8", crate::ids::hex(6)));
        std::fs::write(&file, text).unwrap();
        (file, pair.public_key().as_ref().to_vec())
    }

    #[test]
    fn a_token_is_signed_with_the_key_and_says_whose_it_is() {
        use ring::signature::{ECDSA_P256_SHA256_FIXED, UnparsedPublicKey};
        let (file, public) = key_file("signed");
        let key = Key {
            file: file.clone(),
            key_id: "KEY1".to_string(),
            team_id: "TEAM1".to_string(),
            bundle_id: BUNDLE.to_string(),
            environment: "production".to_string(),
        };
        let token = signed(&key, 1_800_000_000).unwrap();
        let parts: Vec<&str> = token.split('.').collect();
        assert_eq!(parts.len(), 3);
        // What it says is the kernel's; the signature is the key's, over the first two parts.
        let (head, claims) = rules::token("TEAM1", "KEY1", 1_800_000_000);
        assert_eq!(
            format!("{}.{}", parts[0], parts[1]),
            rules::to_sign(&head, &claims)
        );
        let fitted = parts[2].replace('-', "+").replace('_', "/");
        let filled = format!("{fitted}{}", "=".repeat((4 - fitted.len() % 4) % 4));
        let signature = python::b64decode(&filled).unwrap();
        assert_eq!(signature.len(), 64);
        let said = format!("{}.{}", parts[0], parts[1]);
        let by = UnparsedPublicKey::new(&ECDSA_P256_SHA256_FIXED, &public);
        by.verify(said.as_bytes(), &signature).unwrap();
        assert!(by.verify(b"something else", &signature).is_err());
        // A file that holds no key, and one that is not there, are said in words without it.
        std::fs::write(
            &file,
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        )
        .unwrap();
        let why = signed(&key, 1_800_000_000).unwrap_err().0;
        assert!(
            why.ends_with("it is no key of the kind the service takes"),
            "{why}"
        );
        std::fs::write(&file, "no key at all\n").unwrap();
        assert!(
            signed(&key, 1)
                .unwrap_err()
                .0
                .ends_with("its file holds no private key")
        );
        std::fs::remove_file(&file).unwrap();
        assert!(
            signed(&key, 1)
                .unwrap_err()
                .0
                .ends_with("its file is not read")
        );
    }

    #[test]
    fn the_key_is_what_the_owners_file_names_whole() {
        let (file, _) = key_file("named");
        let settings =
            std::env::temp_dir().join(format!("clarp-direct-{}.toml", crate::ids::hex(6)));
        let write = |said: &str| std::fs::write(&settings, said).unwrap();
        let owner = OwnerFile::new(Some(settings.clone()));
        // Nothing named, and a part left out: no key.
        write("[tts]\nprovider = \"none\"\n");
        assert_eq!(key(&owner), None);
        write(&format!(
            "[apns]\nkey_path = \"{}\"\nkey_id = \"KEY1\"\n",
            file.display()
        ));
        assert_eq!(key(&owner), None);
        // Named whole: the app and the side of the service are the usual ones unless said.
        let whole = format!(
            "[apns]\nkey_path = \"{}\"\nkey_id = \" KEY1 \"\nteam_id = \"TEAM1\"\n",
            file.display()
        );
        write(&whole);
        let named = key(&owner).unwrap();
        assert_eq!(
            (
                named.key_id.as_str(),
                named.team_id.as_str(),
                named.bundle_id.as_str()
            ),
            ("KEY1", "TEAM1", BUNDLE)
        );
        assert_eq!(
            (named.environment.as_str(), &named.file),
            ("production", &file)
        );
        write(&format!(
            "{whole}bundle_id = \"org.example.app\"\nenvironment = \"Sandbox\"\n"
        ));
        let named = key(&owner).unwrap();
        assert_eq!(
            (named.bundle_id.as_str(), named.environment.as_str()),
            ("org.example.app", "sandbox")
        );
        // A key whose file is gone is no key.
        std::fs::remove_file(&file).unwrap();
        assert_eq!(key(&owner), None);
        std::fs::remove_file(&settings).unwrap();
    }
}
