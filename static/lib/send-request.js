// The request body for POST /send, and the idempotency key that goes in it.
//
// The server dedups a turn on `client_msg_id or trace_id` (lib/turn_dispatch.py)
// and answers 200 either way, writing nothing when it decides the turn is a
// retry. So the key has to identify *one send*, and a trace id does not: it is
// deliberately long-lived — the server echoes it back, the client stamps every
// subsequent client event with it, and it only rolls over when the server mints
// a new one. Leaving client_msg_id off therefore let two consecutive turns share
// one key and the second was silently discarded.
//
// Every send mints its own key; a retry of that same send reuses it, which is
// what makes the server's dedup protective rather than destructive.
//
// The body carries no trace_id at all. The server mints one per turn when the
// field is absent and returns it, which is the only way each turn gets its own.
// Sending the previous turn's id back made the server adopt it, and in-flight
// bookkeeping is keyed on the trace: three messages typed in quick succession
// all arrived as the same trace and each preempted the last, so only the final
// one survived (`turnPreempted killed=X new=X`). The client still adopts the id
// from the response to stamp its own events — that is what a trace is for.

let counter = 0;

// No crypto.randomUUID: it is gated on a secure context, and Clarp is reached
// over plain http on a NetBird IP. The monotonic counter carries uniqueness on
// its own; the clock and the random suffix only keep keys from colliding across
// reloads and across tabs.
export function newClientMsgId(now = Date.now(), rand = Math.random) {
  counter = (counter + 1) % 0x100000000;
  const seq = counter.toString(36);
  const stamp = Math.floor(now).toString(36);
  const salt = Math.floor(rand() * 0x100000000).toString(36);
  return `c-${stamp}-${seq}-${salt}`;
}

export function buildSendBody({
  text,
  session,
  synthesizeAudio = true,
  handsFree = false,
  clientMsgId = '',
} = {}) {
  return {
    text,
    session,
    client_msg_id: clientMsgId || newClientMsgId(),
    synthesize_audio: !!synthesizeAudio,
    hands_free: !!handsFree,
  };
}
