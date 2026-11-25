//! What the Muse lane does: its loop, taking its session, a turn's prompt, steered words, a
//! Stop, and what Muse says.

use super::*;

impl Lane {
    pub(super) fn inspect(&mut self) -> Snapshot {
        let claimed = self.slot.claim().is_some();
        Snapshot {
            thread: self.session.clone(),
            loaded: self.loaded,
            instance: std::process::id(),
            available: !claimed,
            claim: self.slot.claim().cloned(),
            native_turn: match &self.slot {
                Slot::Running { spool, .. } => Some(spool.turn().to_owned()),
                _ => None,
            },
            options: self.own.digest.clone(),
            model: self.given_model.clone(),
            effort: self.effort.clone(),
            idle_ms: self.idle.ms(claimed),
            memory_bytes: self.memory,
        }
    }

    /// Weighs the lane: itself, `muse serve` and its group (issue 761). Done when the lane is
    /// ready and when a turn has ended, never on a turn's path.
    pub(super) fn weigh(&mut self) {
        self.memory = crate::lane_memory::held(std::process::id(), self.server.pid());
    }

    fn hint(&mut self) {
        self.followers
            .retain_mut(|stream| match stream.write(b"\n") {
                Ok(_) => true,
                Err(error) => error.kind() == io::ErrorKind::WouldBlock,
            });
    }

    fn holds(&self, (turn, attempt): (u64, u32)) -> bool {
        self.slot
            .claim()
            .is_some_and(|claim| (claim.turn, claim.attempt) == (turn, attempt))
    }

    fn retirable(&self) -> bool {
        self.slot.claim().is_none()
    }

    fn deadline(&self) -> Option<Instant> {
        if let Some((at, _)) = self.stop_deadline {
            return Some(at);
        }
        self.idle.deadline(self.retirable())
    }

    /// True: the lane leaves (its idle time is over). An error: Muse did not end a stopped turn
    /// within the Stop's time, and was ended where it stood with the turn (D26).
    fn expired(&mut self) -> io::Result<bool> {
        if let Some((at, attempt)) = self.stop_deadline {
            if Instant::now() < at {
                return Ok(false);
            }
            self.stop_deadline = None;
            if self.holds(attempt) {
                self.server.kill_group();
                if let Slot::Running { spool, .. } = &mut self.slot {
                    spool.killed(crate::now_ms())?;
                }
                self.slot = Slot::Free;
                self.hint();
                self.followers.clear();
                self.turn_socket = None;
                return Err(io::Error::other("Muse did not end its turn after a Stop"));
            }
            return Ok(false);
        }
        Ok(self.idle.over(self.retirable()))
    }

    fn next_input(&mut self) -> io::Result<Option<Input>> {
        loop {
            if self
                .stop_deadline
                .is_some_and(|(at, _)| Instant::now() >= at)
                && self.expired()?
            {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "idle"));
            }
            if let Some(input) = self.deferred.pop_front() {
                return Ok(Some(input));
            }
            let Some(at) = self.deadline() else {
                return Ok(self.input.recv().ok());
            };
            match self
                .input
                .recv_timeout(at.saturating_duration_since(Instant::now()))
            {
                Ok(input) => return Ok(Some(input)),
                Err(RecvTimeoutError::Disconnected) => return Ok(None),
                Err(RecvTimeoutError::Timeout) => {
                    if self.expired()? {
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "idle"));
                    }
                }
            }
        }
    }

    pub(super) fn serve(&mut self) -> io::Result<()> {
        loop {
            let input = match self.next_input() {
                Ok(input) => input,
                Err(error) if error.kind() == io::ErrorKind::TimedOut => return Ok(()),
                Err(error) => return Err(error),
            };
            match input {
                Some(Input::Provider(frame)) => self.frame(frame?)?,
                Some(Input::Control(request, stream)) => {
                    if self.control(request, stream)? {
                        return Ok(());
                    }
                }
                Some(Input::Follow(attempt, mut stream)) => {
                    if self.holds(attempt) && self.followers.len() < 8 {
                        let _ = write(&mut stream, &Reply::ok(None));
                        let _ = stream.set_nonblocking(true);
                        self.followers.push(stream);
                    }
                }
                Some(Input::Stop(attempt, grace_ms, mut stream)) => {
                    let reply = match self.stop(attempt, Duration::from_millis(grace_ms)) {
                        Ok(()) => Reply::ok(None),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Steer(attempt, id, text, mut stream)) => {
                    let reply = match self.steer(attempt, &id, &text) {
                        Ok(()) => Reply::ok(None),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Refused(mut stream)) => {
                    let _ = write(&mut stream, &Reply::failed("Unsupported lane turn request"));
                }
                Some(Input::ProviderGone) | None => {
                    return Err(io::Error::other("Muse serve disconnected"));
                }
            }
            if self.stale_login && self.retirable() {
                return Ok(());
            }
        }
    }

    /// True: the lane leaves now.
    fn control(&mut self, request: Request, mut stream: UnixStream) -> io::Result<bool> {
        let result = match request {
            Request::Inspect => Ok(()),
            Request::Retire if self.retirable() => {
                self.retire_reply = Some(stream);
                return Ok(true);
            }
            Request::Retire => Err(io::Error::other("the Muse lane runs a turn")),
            Request::Take => self.take(),
            Request::Prompt {
                claim,
                text,
                model,
                effort,
                token,
            } => self.prompt(claim, text, model, effort, token.as_deref()),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
            // Nothing reached Muse: the Host starts a new lane and offers the attempt again.
            Err(error) if self.stale_login => {
                json!({"ok": false, "error": error.to_string(), "code": LOGIN_CHANGED})
            }
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        let _ = write(&mut stream, &reply);
        Ok(false)
    }

    /// Takes the session: Muse loads it, and holds its lock, for this lane from now on. Its
    /// tools run without asking, as exec's `--yolo` runs them.
    fn take(&mut self) -> io::Result<()> {
        if self.loaded {
            return Ok(());
        }
        let resumed = self.rpc_within(
            "session/resume",
            json!({"commandId": self.command()?, "sessionId": self.session, "excludeItems": true}),
            LOAD_LIMIT,
        )?;
        self.loaded = true;
        if let Some(model) = resumed["session"]["modelId"].as_str() {
            self.running_model = Some(model.to_owned());
        }
        let mode =
            json!({"commandId": self.command()?, "sessionId": self.session, "mode": "allowAll"});
        self.rpc("session/setApprovalMode", mode)?;
        Ok(())
    }

    fn prompt(
        &mut self,
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
        token: Option<&str>,
    ) -> io::Result<()> {
        if !matches!(self.slot, Slot::Free) {
            return Err(io::Error::other("the Muse lane already runs a turn"));
        }
        if !self.login.acquire()? {
            self.stale_login = true;
            return Err(io::Error::other("Muse's login changed"));
        }
        self.take()?;
        if let Some(model) = model.filter(|m| Some(m) != self.given_model.as_ref()) {
            let selection = json!({"commandId": self.command()?, "sessionId": self.session,
                "model": {"modelId": model}});
            self.rpc("session/setModel", selection)?;
            self.given_model = Some(model.clone());
            self.running_model = Some(model);
        }
        // The spool is made before anything can hand the attempt to Muse: a Host that asks
        // again knows by it that this attempt was taken.
        let pending = ClaimSpool::create(&claim.spool, crate::now_ms())?;
        self.own.keep_token(token, "Muse");
        self.idle.reset();
        // Muse takes steered words over MSP, never on an input of its own.
        self.turn_socket = Some(Socket::turn(&claim, self.send.clone(), Input::turn, false)?);
        let command = self.command()?;
        let mut start = json!({"commandId": command, "sessionId": self.session,
            "input": [{"type": "text", "text": text}]});
        if let Some(effort) = &effort {
            start["reasoningEffort"] = json!(effort);
        }
        self.effort = effort;
        self.slot = Slot::Pending {
            claim,
            spool: pending,
        };
        let started = self.rpc("turn/start", start);
        let Slot::Pending { claim, mut spool } = std::mem::replace(&mut self.slot, Slot::Free)
        else {
            return Ok(());
        };
        let turn = match started {
            Ok(ack) => ack["turnId"].as_str().unwrap_or(&command).to_owned(),
            Err(error) => {
                refused(&mut spool, &error.to_string())?;
                self.clear_slot();
                return Err(error);
            }
        };
        let spool = TurnSpool::begin(
            spool,
            self.session.clone(),
            turn,
            self.server.pid(),
            self.running_model.clone(),
            crate::now_ms(),
        )?;
        self.slot = Slot::Running { claim, spool };
        if let (Some(allowance), Slot::Running { spool, .. }) = (&self.allowance, &mut self.slot) {
            spool.allowance(allowance);
        }
        for (method, params) in std::mem::take(&mut self.early) {
            self.notice(&method, &params)?;
        }
        self.hint();
        Ok(())
    }

    /// More of the user's words for the running turn. Muse refuses them when that turn is no
    /// longer the running one (`expectedTurnId`), and they then wait for a turn of their own.
    fn steer(&mut self, attempt: (u64, u32), id: &str, text: &str) -> io::Result<()> {
        if !self.holds(attempt) || self.stop_deadline.is_some() {
            return Err(io::Error::other("the Muse lane does not run this attempt"));
        }
        let Slot::Running { spool, .. } = &self.slot else {
            return Err(io::Error::other("the Muse turn has not started"));
        };
        if id.is_empty() || text.trim().is_empty() {
            return Err(io::Error::other("steered words need an id and a text"));
        }
        let turn = spool.turn().to_owned();
        let command = self.command()?;
        self.rpc(
            "turn/steer",
            json!({"commandId": command, "sessionId": self.session, "expectedTurnId": turn,
                "input": [{"type": "text", "text": text}]}),
        )?;
        if let Slot::Running { spool, .. } = &mut self.slot {
            spool.steered(&command, id);
        }
        Ok(())
    }

    /// A Stop: steered words Muse has not read are taken back first, or Muse would run them as
    /// a turn of its own after the Stop (measured 2 of 2 on Muse 1.4.25); they go back to the
    /// Host unread. Then the turn is interrupted, and Muse has the Stop's time to end it.
    fn stop(&mut self, attempt: (u64, u32), grace: Duration) -> io::Result<()> {
        if !self.holds(attempt) || self.stop_deadline.is_some() {
            return Ok(());
        }
        let Slot::Running { spool, .. } = &mut self.slot else {
            return Ok(());
        };
        spool.stopping();
        let turn = spool.turn().to_owned();
        self.give_back_unread();
        let command = self.command()?;
        let interrupted = self.rpc(
            "turn/interrupt",
            json!({"commandId": command, "sessionId": self.session, "turnId": turn}),
        );
        let grace = if interrupted.is_ok() {
            grace
        } else {
            Duration::ZERO
        };
        self.stop_deadline = Some((Instant::now() + grace, attempt));
        interrupted.map(|_| ())
    }

    /// Takes back every steered message Muse accepted and has not read: it never runs them, the
    /// Host does, as words never read. One Muse has already begun as its own turn is
    /// interrupted and retracted when that turn begins.
    fn give_back_unread(&mut self) {
        let Slot::Running { spool, .. } = &self.slot else {
            return;
        };
        for steered in spool.unread() {
            let taken = self.command().and_then(|command| {
                self.rpc(
                    "turn/unqueue",
                    json!({"commandId": command, "sessionId": self.session, "turnId": steered}),
                )
            });
            if taken.is_err() {
                self.cancel_on_start.insert(steered.clone());
            }
            if let Slot::Running { spool, .. } = &mut self.slot {
                spool.given_back(&steered);
            }
        }
    }

    fn frame(&mut self, frame: Frame) -> io::Result<()> {
        match frame {
            Frame::Notification { method, params } => self.notice(&method, &params),
            Frame::Request { id } => self.server.refuse_server_request(id),
            Frame::Reply { id, result } => {
                if self.stray.len() < STRAY_MOST {
                    self.stray.insert(id, result);
                }
                Ok(())
            }
        }
    }

    fn notice(&mut self, method: &str, params: &Value) -> io::Result<()> {
        if method == "usage/changed" {
            self.allowance = Some(params.clone());
            if let Slot::Running { spool, .. } = &mut self.slot {
                spool.allowance(params);
            }
            return Ok(());
        }
        if params["sessionId"].as_str() != Some(self.session.as_str()) {
            return Ok(());
        }
        if method == "turn/started"
            && let Some(turn) = params["turnId"].as_str()
            && self.cancel_on_start.remove(turn)
        {
            let command = self.command()?;
            let _ = self.rpc(
                "turn/interrupt",
                json!({"commandId": command, "sessionId": self.session, "turnId": turn,
                    "retract": true}),
            );
            return Ok(());
        }
        if method == "session/closed" {
            self.loaded = false;
            if let Slot::Running { spool, .. } = &mut self.slot {
                spool.lost("Muse closed the session", crate::now_ms())?;
                self.clear_slot();
            }
            return Err(io::Error::other("Muse closed the session"));
        }
        match &mut self.slot {
            Slot::Free => Ok(()),
            Slot::Pending { .. } => {
                if self.early.len() < BUFFER_MOST {
                    self.early.push((method.to_owned(), params.clone()));
                }
                Ok(())
            }
            Slot::Running { spool, .. } => {
                // Words Muse accepted and did not read before the turn's end go back to the
                // Host before the end is written, so they are never read twice or lost.
                if method == "turn/completed" && params["turnId"].as_str() == Some(spool.turn()) {
                    self.give_back_unread();
                }
                let Slot::Running { spool, .. } = &mut self.slot else {
                    return Ok(());
                };
                if spool.notification(method, params, crate::now_ms())? {
                    self.clear_slot();
                } else {
                    self.hint();
                }
                Ok(())
            }
        }
    }

    fn clear_slot(&mut self) {
        self.slot = Slot::Free;
        self.idle.reset();
        self.stop_deadline = None;
        self.hint();
        self.followers.clear();
        self.turn_socket = None;
        self.login.release();
        self.weigh();
    }

    fn command(&self) -> io::Result<String> {
        command_id(crate::now_ms()).ok_or_else(|| io::Error::other("no randomness for an id"))
    }

    fn rpc(&mut self, method: &str, params: Value) -> io::Result<Value> {
        self.rpc_within(method, params, RPC_LIMIT)
    }

    fn rpc_within(&mut self, method: &str, params: Value, limit: Duration) -> io::Result<Value> {
        let id = self.server.request(method, params)?;
        self.response(id, limit)
    }

    /// Waits for the reply to request `id`, handling what Muse says meanwhile and keeping the
    /// Host's requests for afterwards.
    pub(super) fn response(&mut self, id: u64, limit: Duration) -> io::Result<Value> {
        let deadline = Instant::now() + limit;
        loop {
            if let Some(result) = self.stray.remove(&id) {
                return result.map_err(io::Error::other);
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Muse request timed out",
                ));
            };
            match self.input.recv_timeout(remaining) {
                Ok(Input::Provider(Ok(Frame::Reply { id: found, result }))) if found == id => {
                    return result.map_err(io::Error::other);
                }
                Ok(Input::Provider(frame)) => self.frame(frame?)?,
                Ok(Input::ProviderGone) => {
                    return Err(io::Error::other("Muse serve disconnected"));
                }
                Ok(event) if self.deferred.len() < BUFFER_MOST => self.deferred.push_back(event),
                Ok(_) => return Err(io::Error::other("Muse lane request backlog exceeded")),
                Err(error) => return Err(io::Error::other(error)),
            }
        }
    }
}

/// Muse refused the prompt: the attempt's spool says so and ends, once.
fn refused(spool: &mut ClaimSpool, why: &str) -> io::Result<()> {
    let now = crate::now_ms();
    spool.out_json(
        &json!({"payload_type": "run.terminal.failed",
            "payload": {"terminal": "failed", "reason": why}}),
        now,
    )?;
    spool.turn_failed(why, now)?;
    spool.exit(Some(1), None, now)
}
