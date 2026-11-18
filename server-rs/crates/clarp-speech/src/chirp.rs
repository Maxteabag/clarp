//! Google's Chirp: an engine that listens, reached over the network.
//!
//! Chirp takes whole clips: the phone decides where a turn of speech ends, and the Host sends
//! each finished clip to be recognised. It is the engine that gets short Norwegian clips
//! right, is told a set of languages a speaker moves between, and returns nothing for a
//! clip in which nobody speaks. The request is the Python Host's (`server/lib/google_stt.py`),
//! and so are the words it fails with.
//!
//! Google's speech interface takes no key. The engine speaks as the owner's own account:
//! the file `gcloud auth application-default login` wrote, which holds a token that renews
//! itself. That file is read where it is each time a token is needed, and nothing of it is
//! kept but the short-lived token it buys, in memory, until shortly before it runs out.
//!
//! One request takes at most a minute of sound. A clip is decoded once to plain sound (16
//! kHz, one channel) and a longer one is sent as pieces of 55 seconds, a few at a time, their
//! texts joined in order.

use std::collections::HashMap;
use std::fmt::Write as _;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, PoisonError, mpsc};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::listen::{Asked, Clip, Deaf, Heard, Listener, strip};

/// Where a token that renews itself buys a short-lived one.
pub const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";

/// The sound a clip is decoded to: so many samples a second, 16 bits each, one channel.
pub const SAMPLE_RATE: u32 = 16_000;
const BYTES_PER_SECOND: usize = SAMPLE_RATE as usize * 2;

/// How much of a clip goes into one request.
pub const PIECE_SECONDS: usize = 55;

/// How many pieces of one clip are asked for at once.
const AT_ONCE: usize = 4;

/// The languages as Chirp names them. A code that is not listed is passed on as it is, and
/// so is one that is such a name already (`en-GB`).
const NAMED: [(&str, &str); 20] = [
    ("no", "nb-NO"),
    ("nb", "nb-NO"),
    ("nn", "nn-NO"),
    ("en", "en-US"),
    ("sv", "sv-SE"),
    ("da", "da-DK"),
    ("de", "de-DE"),
    ("fi", "fi-FI"),
    ("fr", "fr-FR"),
    ("es", "es-ES"),
    ("it", "it-IT"),
    ("nl", "nl-NL"),
    ("pl", "pl-PL"),
    ("pt", "pt-BR"),
    ("is", "is-IS"),
    ("ja", "ja-JP"),
    ("ko", "ko-KR"),
    ("zh", "cmn-Hans-CN"),
    ("uk", "uk-UA"),
    ("ru", "ru-RU"),
];

/// A language as Chirp is asked for it.
pub fn language_tag(code: &str) -> String {
    let code = strip(code);
    if code.contains('-') {
        return code.to_string();
    }
    let small = code.to_lowercase();
    match NAMED.iter().find(|(known, _)| *known == small) {
        Some((_, tag)) => (*tag).to_string(),
        None if small.is_empty() => "en-US".to_string(),
        None => small,
    }
}

/// The languages a clip is asked to be heard in: one, or the set a speaker moves between,
/// each once.
pub fn languages_asked(language: &str) -> Vec<String> {
    let mut asked: Vec<String> = Vec::new();
    for code in language_tag(language).split(',') {
        let tag = language_tag(code);
        if !asked.contains(&tag) {
            asked.push(tag);
        }
    }
    asked
}

/// Plain sound cut into files of at most `seconds` each, in order: each with the few bytes
/// in front that say what it is (a WAV file).
pub fn pieces(sound: &[u8], seconds: usize) -> Vec<Vec<u8>> {
    let step = (seconds * BYTES_PER_SECOND).max(1);
    sound
        .chunks(step)
        .map(|piece| {
            let length = u32::try_from(piece.len()).unwrap_or(u32::MAX);
            let mut file = Vec::with_capacity(piece.len() + 44);
            file.extend_from_slice(b"RIFF");
            file.extend_from_slice(&length.saturating_add(36).to_le_bytes());
            file.extend_from_slice(b"WAVEfmt ");
            file.extend_from_slice(&16_u32.to_le_bytes());
            // Plain samples, one channel.
            file.extend_from_slice(&1_u16.to_le_bytes());
            file.extend_from_slice(&1_u16.to_le_bytes());
            file.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
            file.extend_from_slice(&(SAMPLE_RATE * 2).to_le_bytes());
            file.extend_from_slice(&2_u16.to_le_bytes());
            file.extend_from_slice(&16_u16.to_le_bytes());
            file.extend_from_slice(b"data");
            file.extend_from_slice(&length.to_le_bytes());
            file.extend_from_slice(piece);
            file
        })
        .collect()
}

/// Bytes as text in base 64.
fn to_base64(bytes: &[u8]) -> String {
    const SIGNS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for three in bytes.chunks(3) {
        let held = [
            three[0],
            three.get(1).copied().unwrap_or(0),
            three.get(2).copied().unwrap_or(0),
        ];
        let sixes = [
            held[0] >> 2,
            ((held[0] & 0x03) << 4) | (held[1] >> 4),
            ((held[1] & 0x0f) << 2) | (held[2] >> 6),
            held[2] & 0x3f,
        ];
        for (place, six) in sixes.iter().enumerate() {
            if place <= three.len() {
                out.push(char::from(SIGNS[usize::from(*six)]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// Names and values as a form is sent: what is no letter, digit or one of `_.-~` by its
/// number, a space as a plus.
pub(crate) fn form_encoded(fields: &[(&str, &str)]) -> String {
    let encoded = |text: &str| -> String {
        let mut out = String::with_capacity(text.len());
        for byte in text.bytes() {
            match byte {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                    out.push(char::from(byte));
                }
                b' ' => out.push('+'),
                other => {
                    let _ = write!(out, "%{other:02X}");
                }
            }
        }
        out
    };
    let pairs = fields.iter();
    pairs
        .map(|(name, value)| format!("{}={}", encoded(name), encoded(value)))
        .collect::<Vec<_>>()
        .join("&")
}

/// The account the engine speaks as, and where: read where the owner keeps them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Account {
    /// The file of the owner's own account (`application_default_credentials.json`).
    pub credentials: PathBuf,
    /// The project that is asked, and that pays.
    pub project: String,
    /// Where it is asked (`eu`).
    pub location: String,
}

/// Where the account is read when a clip is to be heard. `None`: this Host has none.
pub type Asks = Box<dyn Fn() -> Option<Account> + Send + Sync>;

pub struct Chirp {
    account: Asks,
    /// The model, by Google's own name (`chirp_3`).
    model: String,
    token_url: String,
    /// A stand-in's address in place of Google's own.
    speech_at: Option<String>,
    /// The program that turns a clip into plain sound, and what it is given.
    decoder: Vec<String>,
    /// How much of a clip goes into one request, in seconds.
    piece_seconds: usize,
    timeout: Duration,
    /// The short-lived tokens that were bought, by the file that bought them, each with
    /// when it runs out.
    tokens: Mutex<HashMap<PathBuf, (String, Instant)>>,
    /// Held while a token is bought: the pieces of one clip are asked for together, and
    /// only the first of them buys.
    buying: Mutex<()>,
}

/// What an answer that is no success is said to be: the status and the beginning of what
/// came with it.
fn refused(what: &str, status: u16, said: &str) -> String {
    let detail: String = said.chars().take(300).collect();
    let detail = match detail.as_str() {
        "" => "no reason given",
        detail => detail,
    };
    format!("{what} HTTP {status}: {detail}")
}

pub(crate) fn pcm_decoder()->Vec<String> { [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ].into_iter().map(str::to_string).collect() }

    /// A clip as plain sound, whatever it was recorded as.
    pub(crate) fn pcm(decoder:&[String], audio: &[u8]) -> Result<Vec<u8>, String> {
        let Some((program, given)) = decoder.split_first() else {
            return Err("ffmpeg is not installed".to_string());
        };
        let started = Command::new(program)
            .args(given)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn();
        let mut child = match started {
            Ok(child) => child,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Err("ffmpeg is not installed".to_string());
            }
            Err(error) => return Err(format!("could not decode audio: {error}")),
        };
        let (mut to, mut from, mut complaints) =
            match (child.stdin.take(), child.stdout.take(), child.stderr.take()) {
                (Some(to), Some(from), Some(complaints)) => (to, from, complaints),
                _ => return Err("could not decode audio: the decoder has no pipes".to_string()),
            };
        let (done, is_done) = mpsc::channel();
        let outcome = std::thread::scope(|scope| {
            // The clip goes in while the sound comes out: neither waits for the other.
            scope.spawn(move || {
                let _ = to.write_all(audio);
            });
            scope.spawn(move || {
                let mut sound = Vec::new();
                let read = from.read_to_end(&mut sound).map(|_| sound);
                let mut said = String::new();
                let _ = complaints.read_to_string(&mut said);
                let _ = done.send((read, said));
            });
            let outcome = is_done.recv_timeout(Duration::from_secs(30));
            if outcome.is_err() {
                // It takes too long: it is ended, which ends both pipes.
                let _ = child.kill();
            }
            outcome
        });
        let ended = child.wait();
        match (outcome, ended) {
            (Ok((Ok(sound), _)), Ok(status)) if status.success() => Ok(sound),
            (Ok((_, said)), Ok(_)) => {
                let said: String = strip(&said).chars().take(200).collect();
                Err(format!("could not decode audio: {said}"))
            }
            (Err(_), _) => Err("could not decode audio: timed out".to_string()),
            (_, Err(error)) => Err(format!("could not decode audio: {error}")),
        }
    }


impl Chirp {
    pub fn new(model: impl Into<String>, account: Asks) -> Self {
        Self {
            account,
            model: model.into(),
            token_url: TOKEN_URL.to_string(),
            speech_at: None,
            decoder: pcm_decoder(),
            piece_seconds: PIECE_SECONDS,
            timeout: Duration::from_secs(60),
            tokens: Mutex::default(),
            buying: Mutex::default(),
        }
    }

    /// The same engine at other addresses: stand-ins that answer as Google does, one for
    /// tokens and one for speech.
    pub fn at(mut self, token_url: impl Into<String>, speech: impl Into<String>) -> Self {
        self.token_url = token_url.into();
        self.speech_at = Some(speech.into());
        self
    }

    /// The same engine with another program for its decoder.
    pub fn decoded_by(mut self, decoder: Vec<String>) -> Self {
        self.decoder = decoder;
        self
    }

    /// The same engine with other pieces.
    pub fn in_pieces_of(mut self, seconds: usize) -> Self {
        self.piece_seconds = seconds;
        self
    }

    /// One request, answered with JSON. `Err` is why not, in the Python Host's words.
    fn post(
        url: &str,
        headers: &[(&str, &str)],
        body: &[u8],
        timeout: Duration,
        what: &str,
    ) -> Result<Value, String> {
        let config = ureq::Agent::config_builder()
            .max_redirects(0)
            .timeout_global(Some(timeout))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let mut request = agent.post(url);
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        // How it failed, and nothing of what was sent.
        let failed = |error: &dyn std::fmt::Display| format!("{what} request failed: {error}");
        let mut response = request.send(body).map_err(|error| failed(&error))?;
        let status = response.status().as_u16();
        let said = response
            .body_mut()
            .read_to_string()
            .map_err(|error| failed(&error))?;
        if !(200..300).contains(&status) {
            return Err(refused(what, status, &said));
        }
        serde_json::from_str(&said).map_err(|error| failed(&error))
    }

    fn tokens(&self) -> std::sync::MutexGuard<'_, HashMap<PathBuf, (String, Instant)>> {
        self.tokens.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A short-lived token for the owner's account: the one that was bought before while
    /// it lasts another minute, or a new one.
    fn token(&self, credentials: &Path) -> Result<String, String> {
        // One buys at a time: whoever waited finds the token the first one bought.
        let _one_at_a_time = self.buying.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some((token, runs_out)) = self.tokens().get(credentials)
            && *runs_out > Instant::now() + Duration::from_secs(60)
        {
            return Ok(token.clone());
        }
        let (token, lasts) = Self::refresh_token(credentials, &self.token_url)?;
        let runs_out = Instant::now() + Duration::from_secs_f64(lasts.unwrap_or(3_600.0));
        self.tokens()
            .insert(credentials.to_path_buf(), (token.clone(), runs_out));
        Ok(token)
    }

    /// Exchange an authorized-user ADC refresh token for a short-lived access token.
    pub(crate) fn refresh_token(
        credentials: &Path,
        token_url: &str,
    ) -> Result<(String, Option<f64>), String> {
        let read = std::fs::read_to_string(credentials)
            .map_err(|error| error.to_string())
            .and_then(|text| {
                serde_json::from_str::<Value>(&text).map_err(|error| error.to_string())
            });
        let account = read.map_err(|error| format!("cannot read Google credentials: {error}"))?;
        if account["type"] != "authorized_user" {
            return Err("Google credentials must be an authorized_user ADC file \
                        (gcloud auth application-default login)"
                .to_string());
        }
        let of = |name: &str| account[name].as_str().unwrap_or("").to_string();
        let (renews, client, secret) = (of("refresh_token"), of("client_id"), of("client_secret"));
        let form = form_encoded(&[
            ("grant_type", "refresh_token"),
            ("refresh_token", &renews),
            ("client_id", &client),
            ("client_secret", &secret),
        ]);
        let kind = [("Content-Type", "application/x-www-form-urlencoded")];
        let bought = Self::post(
            token_url,
            &kind,
            form.as_bytes(),
            Duration::from_secs(15),
            "Google token",
        )?;
        let Some(token) = bought["access_token"]
            .as_str()
            .filter(|token| !token.is_empty())
        else {
            return Err("Google token response had no access_token".to_string());
        };
        let lasts = bought["expires_in"].as_f64().filter(|lasts| *lasts > 0.0);
        Ok((token.to_string(), lasts))
    }

    /// The text of one piece. A token that is refused is bought anew once, and a request
    /// that is stuck or meets a busy provider is made once more.
    fn recognised(
        &self,
        piece: &[u8],
        account: &Account,
        languages: &[String],
    ) -> Result<String, String> {
        let host = match account.location.as_str() {
            "global" => "speech.googleapis.com".to_string(),
            location => format!("{location}-speech.googleapis.com"),
        };
        let base = match &self.speech_at {
            Some(stand_in) => stand_in.trim_end_matches('/').to_string(),
            None => format!("https://{host}"),
        };
        let url = format!(
            "{base}/v2/projects/{}/locations/{}/recognizers/_:recognize",
            account.project, account.location
        );
        let body = json!({
            "config": {
                "autoDecodingConfig": {},
                "model": self.model,
                "languageCodes": languages,
                "features": { "enableAutomaticPunctuation": true },
            },
            "content": to_base64(piece),
        })
        .to_string();
        // Chirp answers a piece in a second or two, so a long wait is a request that is
        // stuck: it is given up early and made once more.
        let seconds = piece.len() as f64 / BYTES_PER_SECOND as f64;
        let patience =
            Duration::from_secs_f64((8.0 + seconds / 5.0).min(self.timeout.as_secs_f64()));
        let mut answer = Err(String::new());
        for attempt in 0..2 {
            let token = self.token(&account.credentials)?;
            let bearer = format!("Bearer {token}");
            let headers = [
                ("Authorization", bearer.as_str()),
                ("x-goog-user-project", account.project.as_str()),
                ("Content-Type", "application/json"),
            ];
            answer = Self::post(&url, &headers, body.as_bytes(), patience, "Google STT");
            let Err(why) = &answer else { break };
            let refused_token = why.contains("HTTP 401");
            if refused_token {
                self.tokens().remove(&account.credentials);
            }
            let stuck =
                why.contains("timed out") || why.contains("HTTP 5") || why.contains("HTTP 429");
            if attempt == 1 || !(refused_token || stuck) {
                break;
            }
        }
        let answer = answer?;
        let mut texts = Vec::new();
        for result in answer["results"].as_array().into_iter().flatten() {
            if let Some(text) = result["alternatives"][0]["transcript"].as_str() {
                let text = strip(text);
                if !text.is_empty() {
                    texts.push(text.to_string());
                }
            }
        }
        Ok(texts.join(" "))
    }
}

impl Listener for Chirp {
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf> {
        let Some(account) = (self.account)() else {
            let none = "Google credentials are not configured";
            return Err(Deaf::Failed(none.to_string()));
        };
        if account.project.is_empty() {
            let none = "[google_stt] project is not configured";
            return Err(Deaf::Failed(none.to_string()));
        }
        let sound = pcm(&self.decoder,clip.audio).map_err(Deaf::Failed)?;
        let seconds = sound.len() as f64 / BYTES_PER_SECOND as f64;
        if sound.is_empty() {
            return Ok(Heard::default());
        }
        let languages = languages_asked(asked.language.unwrap_or("en"));
        let files = pieces(&sound, self.piece_seconds);
        let mut texts: Vec<String> = Vec::with_capacity(files.len());
        // A few pieces at a time, and their texts in the order of the clip.
        for together in files.chunks(AT_ONCE) {
            let heard: Vec<Result<String, String>> = std::thread::scope(|scope| {
                let asked: Vec<_> = together
                    .iter()
                    .map(|piece| scope.spawn(|| self.recognised(piece, &account, &languages)))
                    .collect();
                let cut_off = || Err("Google STT request failed: its thread ended".to_string());
                asked
                    .into_iter()
                    .map(|piece| piece.join().unwrap_or_else(|_| cut_off()))
                    .collect()
            });
            for text in heard {
                texts.push(text.map_err(Deaf::Failed)?);
            }
        }
        texts.retain(|text| !text.is_empty());
        Ok(Heard::text(texts.join(" "), seconds))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::net::{TcpListener, TcpStream};
    use std::sync::Arc;

    use super::*;

    fn vectors() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/chirp-vectors.json")).unwrap()
    }

    fn sha256_hex(bytes: &[u8]) -> String {
        // The fixture names each piece by its SHA-256, and this crate has no hasher: the
        // few lines of it are here for the test alone.
        const K: [u32; 64] = [
            0x428a_2f98,
            0x7137_4491,
            0xb5c0_fbcf,
            0xe9b5_dba5,
            0x3956_c25b,
            0x59f1_11f1,
            0x923f_82a4,
            0xab1c_5ed5,
            0xd807_aa98,
            0x1283_5b01,
            0x2431_85be,
            0x550c_7dc3,
            0x72be_5d74,
            0x80de_b1fe,
            0x9bdc_06a7,
            0xc19b_f174,
            0xe49b_69c1,
            0xefbe_4786,
            0x0fc1_9dc6,
            0x240c_a1cc,
            0x2de9_2c6f,
            0x4a74_84aa,
            0x5cb0_a9dc,
            0x76f9_88da,
            0x983e_5152,
            0xa831_c66d,
            0xb003_27c8,
            0xbf59_7fc7,
            0xc6e0_0bf3,
            0xd5a7_9147,
            0x06ca_6351,
            0x1429_2967,
            0x27b7_0a85,
            0x2e1b_2138,
            0x4d2c_6dfc,
            0x5338_0d13,
            0x650a_7354,
            0x766a_0abb,
            0x81c2_c92e,
            0x9272_2c85,
            0xa2bf_e8a1,
            0xa81a_664b,
            0xc24b_8b70,
            0xc76c_51a3,
            0xd192_e819,
            0xd699_0624,
            0xf40e_3585,
            0x106a_a070,
            0x19a4_c116,
            0x1e37_6c08,
            0x2748_774c,
            0x34b0_bcb5,
            0x391c_0cb3,
            0x4ed8_aa4a,
            0x5b9c_ca4f,
            0x682e_6ff3,
            0x748f_82ee,
            0x78a5_636f,
            0x84c8_7814,
            0x8cc7_0208,
            0x90be_fffa,
            0xa450_6ceb,
            0xbef9_a3f7,
            0xc671_78f2,
        ];
        let mut state: [u32; 8] = [
            0x6a09_e667,
            0xbb67_ae85,
            0x3c6e_f372,
            0xa54f_f53a,
            0x510e_527f,
            0x9b05_688c,
            0x1f83_d9ab,
            0x5be0_cd19,
        ];
        let mut padded = bytes.to_vec();
        padded.push(0x80);
        while padded.len() % 64 != 56 {
            padded.push(0);
        }
        padded.extend_from_slice(&(bytes.len() as u64 * 8).to_be_bytes());
        for block in padded.chunks(64) {
            let mut words = [0u32; 64];
            for (at, four) in block.chunks(4).enumerate() {
                words[at] = u32::from_be_bytes([four[0], four[1], four[2], four[3]]);
            }
            for at in 16..64 {
                let s0 = words[at - 15].rotate_right(7)
                    ^ words[at - 15].rotate_right(18)
                    ^ (words[at - 15] >> 3);
                let s1 = words[at - 2].rotate_right(17)
                    ^ words[at - 2].rotate_right(19)
                    ^ (words[at - 2] >> 10);
                words[at] = words[at - 16]
                    .wrapping_add(s0)
                    .wrapping_add(words[at - 7])
                    .wrapping_add(s1);
            }
            let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = state;
            for at in 0..64 {
                let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
                let choice = (e & f) ^ (!e & g);
                let one = h
                    .wrapping_add(s1)
                    .wrapping_add(choice)
                    .wrapping_add(K[at])
                    .wrapping_add(words[at]);
                let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
                let majority = (a & b) ^ (a & c) ^ (b & c);
                let two = s0.wrapping_add(majority);
                (h, g, f, e, d, c, b, a) =
                    (g, f, e, d.wrapping_add(one), c, b, a, one.wrapping_add(two));
            }
            for (held, new) in state.iter_mut().zip([a, b, c, d, e, f, g, h]) {
                *held = held.wrapping_add(new);
            }
        }
        state.iter().map(|word| format!("{word:08x}")).collect()
    }

    #[test]
    fn a_language_is_named_and_a_clip_is_cut_as_the_python_host_does() {
        let vectors = vectors();
        assert_eq!(vectors["sample_rate"], SAMPLE_RATE);
        assert_eq!(vectors["piece_seconds"], PIECE_SECONDS);
        assert_eq!(vectors["token_url"], TOKEN_URL);
        let tags = vectors["tags"].as_array().unwrap();
        assert!(tags.len() >= 18);
        for case in tags {
            let language = case["language"].as_str().unwrap();
            assert_eq!(
                language_tag(language),
                case["tag"].as_str().unwrap(),
                "{case}"
            );
            let asked: Vec<&str> = case["asked"]
                .as_array()
                .unwrap()
                .iter()
                .map(|tag| tag.as_str().unwrap())
                .collect();
            assert_eq!(languages_asked(language), asked, "{case}");
        }
        let cuts = vectors["pieces"].as_array().unwrap();
        assert!(cuts.len() >= 7);
        for case in cuts {
            let length = usize::try_from(case["bytes"].as_u64().unwrap()).unwrap();
            let seconds = usize::try_from(case["seconds"].as_u64().unwrap()).unwrap();
            let sound: Vec<u8> = (0..length).map(|at| (at % 251) as u8).collect();
            let made: Vec<(u64, String)> = pieces(&sound, seconds)
                .iter()
                .map(|piece| (piece.len() as u64, sha256_hex(piece)))
                .collect();
            let wanted: Vec<(u64, String)> = case["pieces"]
                .as_array()
                .unwrap()
                .iter()
                .map(|piece| {
                    (
                        piece["bytes"].as_u64().unwrap(),
                        piece["sha256"].as_str().unwrap().to_string(),
                    )
                })
                .collect();
            assert_eq!(
                made, wanted,
                "{} bytes in pieces of {seconds} s",
                case["bytes"]
            );
        }
    }

    #[test]
    fn bytes_are_written_as_text_and_a_form_as_a_form() {
        assert_eq!(to_base64(b""), "");
        assert_eq!(to_base64(b"f"), "Zg==");
        assert_eq!(to_base64(b"fo"), "Zm8=");
        assert_eq!(to_base64(b"foo"), "Zm9v");
        assert_eq!(to_base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(to_base64(&[0xff, 0xfe, 0xfd, 0xfc]), "//79/A==");
        assert_eq!(
            form_encoded(&[
                ("grant_type", "refresh_token"),
                ("refresh_token", "1//a b+c/d=e&f")
            ]),
            "grant_type=refresh_token&refresh_token=1%2F%2Fa+b%2Bc%2Fd%3De%26f"
        );
    }

    /// What a stand-in for Google answers a request with, by its place among the requests.
    type Script = Vec<(&'static str, String)>;

    /// A stand-in for Google on this machine: tokens and speech at one address. It answers
    /// the requests in the order of its script and keeps what it was asked.
    fn stand_in(script: Script) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}", listener.local_addr().unwrap());
        let taken: Arc<Mutex<Vec<String>>> = Arc::default();
        let keeps = taken.clone();
        std::thread::spawn(move || {
            let mut script = script.into_iter();
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { break };
                let request = whole_request(&mut stream);
                keeps.lock().unwrap().push(request);
                let Some((status, body)) = script.next() else {
                    break;
                };
                let reply = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                     Connection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        (address, taken)
    }

    fn whole_request(stream: &mut TcpStream) -> String {
        let (mut seen, mut buffer) = (Vec::new(), [0u8; 8192]);
        loop {
            let text = String::from_utf8_lossy(&seen).to_string();
            if let Some(at) = text.find("\r\n\r\n") {
                let length = text.lines().find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    let named = name.eq_ignore_ascii_case("content-length");
                    named.then(|| value.trim().parse::<usize>().ok())?
                });
                if seen.len() >= at + 4 + length.unwrap_or(0) {
                    return text;
                }
            }
            match stream.read(&mut buffer) {
                Ok(read) if read > 0 => seen.extend_from_slice(&buffer[..read]),
                _ => return String::from_utf8_lossy(&seen).to_string(),
            }
        }
    }

    /// The owner's account in a file of its own, and an engine that asks the stand-in.
    fn engine(address: &str, name: &str) -> (Chirp, PathBuf) {
        let file =
            std::env::temp_dir().join(format!("clarp-chirp-{name}-{}.json", std::process::id()));
        let account = json!({
            "type": "authorized_user", "refresh_token": "renews/it self",
            "client_id": "client-1", "client_secret": "secret-1",
        });
        std::fs::write(&file, account.to_string()).unwrap();
        let (credentials, token_url) = (file.clone(), format!("{address}/token"));
        let asks: Asks = Box::new(move || {
            Some(Account {
                credentials: credentials.clone(),
                project: "project-1".to_string(),
                location: "eu".to_string(),
            })
        });
        // The "clip" is plain sound already: the decoder passes it through.
        let chirp = Chirp::new("chirp_3", asks)
            .at(token_url, address)
            .decoded_by(vec!["cat".to_string()]);
        (chirp, file)
    }

    fn hear(chirp: &Chirp, sound: &[u8], language: Option<&str>) -> Result<Heard, Deaf> {
        let clip = Clip {
            audio: sound,
            content_type: "audio/mp4",
        };
        let asked = Asked {
            listen_for: "Maximus",
            language,
            wait: Duration::from_secs(1),
        };
        chirp.hear(clip, asked)
    }

    fn token(value: &str) -> (&'static str, String) {
        (
            "200 OK",
            json!({ "access_token": value, "expires_in": 3600 }).to_string(),
        )
    }

    fn heard(text: &str) -> (&'static str, String) {
        let answer = json!({ "results": [{ "alternatives": [{ "transcript": text }] }] });
        ("200 OK", answer.to_string())
    }

    #[test]
    fn a_clip_is_heard_as_the_owners_account_in_the_languages_of_its_agent() {
        let (address, taken) = stand_in(vec![
            token("token-1"),
            heard(" Åpne døra. "),
            heard("Open the door."),
        ]);
        let (chirp, file) = engine(&address, "heard");
        let said = hear(&chirp, &[1, 2, 3, 4], Some("no,en")).unwrap();
        assert_eq!(said.segments[0].text, "Åpne døra.");
        assert!((said.seconds - 4.0 / 32_000.0).abs() < 1e-9);
        // The token is kept while it lasts: the next clip buys none.
        let said = hear(&chirp, &[5, 6], None).unwrap();
        assert_eq!(said.segments[0].text, "Open the door.");
        let taken = taken.lock().unwrap().clone();
        assert_eq!(taken.len(), 3);
        // The token was bought with what the owner's file holds, as a form.
        assert!(taken[0].starts_with("POST /token "), "{}", taken[0]);
        assert!(taken[0].ends_with(
            "grant_type=refresh_token&refresh_token=renews%2Fit+self&client_id=client-1&client_secret=secret-1"
        ));
        // The clip was asked of the owner's project, where it is, as that account.
        let first = &taken[1];
        assert!(
            first.starts_with("POST /v2/projects/project-1/locations/eu/recognizers/_:recognize "),
            "{first}"
        );
        let lower = first.to_lowercase();
        assert!(lower.contains("authorization: bearer token-1"));
        assert!(lower.contains("x-goog-user-project: project-1"));
        let body: Value = serde_json::from_str(first.split_once("\r\n\r\n").unwrap().1).unwrap();
        assert_eq!(
            body["config"],
            json!({
                "autoDecodingConfig": {}, "model": "chirp_3", "languageCodes": ["nb-NO", "en-US"],
                "features": { "enableAutomaticPunctuation": true },
            })
        );
        // The sound went as a file of its kind: the few bytes that say so, then the sound.
        let file_sent = pieces(&[1, 2, 3, 4], PIECE_SECONDS).remove(0);
        assert_eq!(body["content"], to_base64(&file_sent));
        // Nobody named a language: English, as the Host's is.
        let body: Value = serde_json::from_str(taken[2].split_once("\r\n\r\n").unwrap().1).unwrap();
        assert_eq!(body["config"]["languageCodes"], json!(["en-US"]));
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn a_token_that_is_refused_is_bought_anew_and_a_busy_provider_is_asked_once_more() {
        let (address, taken) = stand_in(vec![
            token("old"),
            ("401 Unauthorized", r#"{"error": "expired"}"#.to_string()),
            token("new"),
            heard("Second try."),
            ("503 Service Unavailable", "busy".to_string()),
            heard("Third."),
            ("503 Service Unavailable", "busy".to_string()),
            ("503 Service Unavailable", "still busy".to_string()),
            (
                "400 Bad Request",
                r#"{"error": "no such model"}"#.to_string(),
            ),
        ]);
        let (chirp, file) = engine(&address, "again");
        assert_eq!(
            hear(&chirp, &[1, 2], None).unwrap().segments[0].text,
            "Second try."
        );
        assert_eq!(
            hear(&chirp, &[1, 2], None).unwrap().segments[0].text,
            "Third."
        );
        // Busy twice: that is the answer. And what is refused for what was asked is not
        // asked again.
        assert_eq!(
            hear(&chirp, &[1, 2], None),
            Err(Deaf::Failed("Google STT HTTP 503: still busy".to_string()))
        );
        assert_eq!(
            hear(&chirp, &[1, 2], None),
            Err(Deaf::Failed(
                r#"Google STT HTTP 400: {"error": "no such model"}"#.to_string()
            ))
        );
        let taken = taken.lock().unwrap().clone();
        let asked: Vec<&str> = taken
            .iter()
            .map(|request| request.split(' ').nth(1).unwrap())
            .collect();
        let speech = "/v2/projects/project-1/locations/eu/recognizers/_:recognize";
        assert_eq!(
            asked,
            [
                "/token", speech, "/token", speech, speech, speech, speech, speech, speech
            ]
        );
        assert!(
            taken[3]
                .to_lowercase()
                .contains("authorization: bearer new")
        );
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn a_long_clip_is_heard_in_pieces_and_a_silent_one_is_nothing() {
        let (address, taken) =
            stand_in(vec![token("t"), heard("One."), heard(""), heard("Three.")]);
        let (chirp, file) = engine(&address, "pieces");
        let chirp = chirp.in_pieces_of(1);
        let sound = vec![7u8; 2 * BYTES_PER_SECOND + 10];
        // The pieces are asked for together, so which request the stand-in answers with
        // which text is not the order of the clip; all three texts are heard, without the
        // empty one.
        let said = hear(&chirp, &sound, Some("en")).unwrap();
        let mut words: Vec<&str> = said.segments[0].text.split(' ').collect();
        words.sort_unstable();
        assert_eq!(words, ["One.", "Three."]);
        assert_eq!(taken.lock().unwrap().len(), 4);
        // No sound at all: nothing is asked, and nothing was said.
        assert_eq!(hear(&chirp, &[], None), Ok(Heard::default()));
        assert_eq!(taken.lock().unwrap().len(), 4);
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn what_is_missing_is_said_in_the_python_hosts_words() {
        let nobody: Asks = Box::new(|| None);
        let chirp = Chirp::new("chirp_3", nobody);
        assert_eq!(
            hear(&chirp, &[1], None),
            Err(Deaf::Failed(
                "Google credentials are not configured".to_string()
            ))
        );
        let account = |credentials: &str, project: &str| -> Asks {
            let (credentials, project) = (PathBuf::from(credentials), project.to_string());
            Box::new(move || {
                Some(Account {
                    credentials: credentials.clone(),
                    project: project.clone(),
                    location: "eu".to_string(),
                })
            })
        };
        let no_project = Chirp::new("chirp_3", account("/nowhere", ""));
        assert_eq!(
            hear(&no_project, &[1], None),
            Err(Deaf::Failed(
                "[google_stt] project is not configured".to_string()
            ))
        );
        let cat = || vec!["cat".to_string()];
        let no_file = Chirp::new("chirp_3", account("/nowhere/at/all.json", "p")).decoded_by(cat());
        let Err(Deaf::Failed(why)) = hear(&no_file, &[1], None) else {
            panic!("a file that is not there was read");
        };
        assert!(why.starts_with("cannot read Google credentials: "), "{why}");
        // A file of another kind of account.
        let other =
            std::env::temp_dir().join(format!("clarp-chirp-other-{}.json", std::process::id()));
        std::fs::write(&other, r#"{"type": "service_account"}"#).unwrap();
        let wrong = Chirp::new("chirp_3", account(other.to_str().unwrap(), "p")).decoded_by(cat());
        assert_eq!(
            hear(&wrong, &[1], None),
            Err(Deaf::Failed(
                "Google credentials must be an authorized_user ADC file \
                 (gcloud auth application-default login)"
                    .to_string()
            ))
        );
        std::fs::remove_file(other).unwrap();
        // No decoder on this machine, and one that fails.
        let none = Chirp::new("chirp_3", account("/nowhere", "p"))
            .decoded_by(vec!["clarp-no-such-decoder".to_string()]);
        assert_eq!(
            hear(&none, &[1], None),
            Err(Deaf::Failed("ffmpeg is not installed".to_string()))
        );
        let fails = Chirp::new("chirp_3", account("/nowhere", "p")).decoded_by(vec![
            "sh".to_string(),
            "-c".to_string(),
            "cat > /dev/null; printf '%s\\n' 'no audio in it' >&2; exit 3".to_string(),
        ]);
        assert_eq!(
            hear(&fails, &[1], None),
            Err(Deaf::Failed(
                "could not decode audio: no audio in it".to_string()
            ))
        );
    }
}
