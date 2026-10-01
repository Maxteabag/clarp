//! MPRIS (C++ `MprisIntegration`): voice replies appear to the desktop as a
//! media player, so media keys and the panel's player controls pause,
//! resume and stop speech, and Raise brings the window forward. Served on
//! the session bus as `org.mpris.MediaPlayer2.Clarp.instance<pid>`.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use zbus::zvariant::{ObjectPath, OwnedValue, Value};

pub const PATH: &str = "/org/mpris/MediaPlayer2";

/// What the window asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Request {
    Raise,
    Quit,
    /// "pause", "resume", "toggle" or "stop" (AudioController commands).
    Playback(&'static str),
}

/// The audio state the player reports.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Status {
    pub playing: bool,
    pub paused: bool,
    pub available: bool,
}

impl Status {
    pub fn playback_status(self) -> &'static str {
        if self.playing {
            "Playing"
        } else if self.paused {
            "Paused"
        } else {
            "Stopped"
        }
    }
}

type Act = Arc<dyn Fn(Request) + Send + Sync>;

struct Root {
    act: Act,
}

#[zbus::interface(name = "org.mpris.MediaPlayer2")]
impl Root {
    fn raise(&self) {
        (self.act)(Request::Raise);
    }
    fn quit(&self) {
        (self.act)(Request::Quit);
    }
    #[zbus(property)]
    fn can_quit(&self) -> bool {
        true
    }
    #[zbus(property)]
    fn can_raise(&self) -> bool {
        true
    }
    #[zbus(property)]
    fn has_track_list(&self) -> bool {
        false
    }
    #[zbus(property)]
    fn identity(&self) -> &str {
        "Clarp"
    }
    #[zbus(property)]
    fn desktop_entry(&self) -> &str {
        "com.maxteabag.Clarp"
    }
    #[zbus(property)]
    fn supported_uri_schemes(&self) -> Vec<String> {
        Vec::new()
    }
    #[zbus(property)]
    fn supported_mime_types(&self) -> Vec<String> {
        vec!["audio/mpeg".into(), "audio/mp4".into()]
    }
}

struct Player {
    act: Act,
    status: Arc<Mutex<Status>>,
}

impl Player {
    fn status(&self) -> Status {
        match self.status.lock() {
            Ok(status) => *status,
            Err(error) => {
                eprintln!("MPRIS: status unavailable: {error}");
                Status::default()
            }
        }
    }
}

#[zbus::interface(name = "org.mpris.MediaPlayer2.Player")]
impl Player {
    fn next(&self) {}
    fn previous(&self) {}
    fn pause(&self) {
        (self.act)(Request::Playback("pause"));
    }
    fn play_pause(&self) {
        (self.act)(Request::Playback("toggle"));
    }
    fn stop(&self) {
        if self.status().available {
            (self.act)(Request::Playback("stop"));
        }
    }
    fn play(&self) {
        (self.act)(Request::Playback("resume"));
    }
    fn seek(&self, _offset: i64) {}
    fn set_position(&self, _track: ObjectPath<'_>, _position: i64) {}
    fn open_uri(&self, _uri: &str) {}

    #[zbus(property)]
    fn playback_status(&self) -> &'static str {
        self.status().playback_status()
    }
    #[zbus(property)]
    fn metadata(&self) -> HashMap<String, OwnedValue> {
        let mut metadata = HashMap::new();
        if !self.status().available {
            return metadata;
        }
        let entries: [(&str, Value<'_>); 3] = [
            ("mpris:trackid", ObjectPath::from_static_str_unchecked("/org/mpris/MediaPlayer2/Track/Voice").into()),
            ("xesam:title", "Clarp voice reply".into()),
            ("xesam:artist", vec!["Clarp"].into()),
        ];
        for (key, value) in entries {
            match OwnedValue::try_from(value) {
                Ok(value) => {
                    metadata.insert(key.to_owned(), value);
                }
                Err(error) => eprintln!("MPRIS: cannot encode {key}: {error}"),
            }
        }
        metadata
    }
    #[zbus(property)]
    fn rate(&self) -> f64 {
        1.0
    }
    #[zbus(property)]
    fn volume(&self) -> f64 {
        1.0
    }
    #[zbus(property)]
    fn position(&self) -> i64 {
        0
    }
    #[zbus(property)]
    fn minimum_rate(&self) -> f64 {
        1.0
    }
    #[zbus(property)]
    fn maximum_rate(&self) -> f64 {
        1.0
    }
    #[zbus(property)]
    fn can_go_next(&self) -> bool {
        false
    }
    #[zbus(property)]
    fn can_go_previous(&self) -> bool {
        false
    }
    #[zbus(property)]
    fn can_play(&self) -> bool {
        self.status().paused && !self.status().playing
    }
    #[zbus(property)]
    fn can_pause(&self) -> bool {
        self.status().playing
    }
    #[zbus(property)]
    fn can_seek(&self) -> bool {
        false
    }
    #[zbus(property)]
    fn can_control(&self) -> bool {
        true
    }
}

/// The player on the session bus, for the window's lifetime.
pub struct Mpris {
    connection: zbus::Connection,
    status: Arc<Mutex<Status>>,
}

impl Mpris {
    pub async fn serve(status: Status, act: impl Fn(Request) + Send + Sync + 'static) -> zbus::Result<Self> {
        let act: Act = Arc::new(act);
        let shared = Arc::new(Mutex::new(status));
        let connection = zbus::connection::Builder::session()?
            .name(format!("org.mpris.MediaPlayer2.Clarp.instance{}", std::process::id()))?
            .serve_at(PATH, Root { act: act.clone() })?
            .serve_at(PATH, Player { act, status: shared.clone() })?
            .build()
            .await?;
        Ok(Self { connection, status: shared })
    }

    /// Records the new audio state and tells the panel (C++
    /// `publishPlaybackStatus`).
    pub fn publish(&self, status: Status) {
        match self.status.lock() {
            Ok(current) if *current == status => return,
            Ok(mut current) => *current = status,
            Err(error) => {
                eprintln!("MPRIS: status unavailable: {error}");
                return;
            }
        }
        let connection = self.connection.clone();
        crate::runtime::handle().spawn(async move {
            // One signal with all four, as the C++ client sends, so a panel
            // never sees Playing without CanPause.
            let changed = async {
                let player = connection.object_server().interface::<_, Player>(PATH).await?;
                let player_ref = player.get().await;
                let metadata = player_ref.metadata();
                let mut values: HashMap<&str, Value<'_>> = HashMap::new();
                values.insert("PlaybackStatus", status.playback_status().into());
                values.insert("Metadata", metadata.into());
                values.insert("CanPlay", player_ref.can_play().into());
                values.insert("CanPause", player_ref.can_pause().into());
                let interface = zbus::names::InterfaceName::from_static_str_unchecked("org.mpris.MediaPlayer2.Player");
                zbus::fdo::Properties::properties_changed(player.signal_emitter(), interface, values, std::borrow::Cow::Borrowed(&[])).await
            };
            if let Err(error) = changed.await {
                eprintln!("MPRIS: could not publish the playback status: {error}");
            }
        });
    }
}
