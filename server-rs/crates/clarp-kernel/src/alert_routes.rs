//! Alert routes for one batch, as selected by the Python push router.

/// What became of one push.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The push service took it.
    Sent,
    /// The push service says the token is gone: nothing is sent to it again.
    Dead(String),
    /// The phone took its grant back, and this Host has no other way to it.
    GrantRevoked,
    /// This one push was refused. Nothing is known against the token.
    Refused { status: u16, reason: String },
    /// The relay could not be reached, or could not reach the push service.
    Down(String),
    /// This Host has no way to the phone: it gave no grant, and the Host has no credential.
    NoPath,
}

/// One push, as it was delivered or not.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub outcome: Outcome,
    /// The phone's grant was refused on the way: it is forgotten, whatever came after.
    pub grant_revoked: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Path {
    Grant,
    Account,
    Direct,
}

pub struct Routes {
    enabled: bool,
    grants: bool,
    base: Option<Path>,
    grant_down: Option<String>,
    account_down: Option<String>,
    fallback: bool,
}

impl Routes {
    pub fn new(mode: &str, relay: bool, account: bool, key: bool) -> Self {
        // The reference normalizes unknown modes in Config.apns_transport only;
        // its per-phone grant selection checks the original mode.
        let relay_allowed = !matches!(mode, "off" | "direct");
        let direct_allowed = !matches!(mode, "off" | "relay");
        let base = if relay_allowed && relay && account {
            Some(Path::Account)
        } else if direct_allowed && key {
            Some(Path::Direct)
        } else {
            None
        };
        Self {
            enabled: base.is_some() || (relay && relay_allowed),
            base,
            grants: relay && matches!(mode, "auto" | "relay"),
            grant_down: None,
            account_down: None,
            fallback: direct_allowed && key,
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub fn send(&mut self, granted: bool, mut send: impl FnMut(Path) -> Outcome) -> Delivery {
        let mut grant_revoked = false;
        let outcome = 'route: {
            if granted && self.grants {
                let answer = self.grant_down.clone().map_or_else(|| send(Path::Grant), Outcome::Down);
                match answer {
                    Outcome::Down(why) => {
                        self.grant_down = Some(why.clone());
                        break 'route if self.fallback { send(Path::Direct) } else { Outcome::Down(why) };
                    }
                    Outcome::GrantRevoked => {
                        grant_revoked = true;
                        if self.base.is_none() {
                            break 'route Outcome::GrantRevoked;
                        }
                    }
                    other => break 'route other,
                }
            }
            match self.base {
                Some(Path::Account) => {
                    let answer = self.account_down.clone().map_or_else(|| send(Path::Account), Outcome::Down);
                    if let Outcome::Down(why) = answer {
                        if self.fallback {
                            self.base = Some(Path::Direct);
                            send(Path::Direct)
                        } else {
                            self.account_down = Some(why.clone());
                            Outcome::Down(why)
                        }
                    } else {
                        answer
                    }
                }
                Some(path) => send(path),
                None => Outcome::NoPath,
            }
        };
        Delivery { outcome, grant_revoked }
    }
}
