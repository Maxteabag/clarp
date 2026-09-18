# Optional typed judgments (TypeSafe Jev)

Clarp decides a lot of small things about language: was that real speech, who is
the user talking to, why did a turn die. Each has deterministic code today — a
regex, a phrase blocklist, an ordered set of patterns. Those are fast and free,
and they are wrong in ways that are hard to fix by adding more patterns: the
blocklist deletes "Okay." along with "Thanks for watching", and the name matcher
cannot tell "Rachel, check the branch" from "did Rachel finish?".

This is an optional second opinion from a model that returns a typed answer with
calibrated probabilities instead of prose. It is off unless you turn it on, and
every site keeps its existing code as the fallback.

## Turning it on

1. Put a key in `config.toml`:

   ```toml
   [typesafe]
   api_key = "..."     # or the TYPESAFE_API_KEY environment variable
   ```

2. Enable the master switch and the sites you want:

   ```bash
   curl -X POST localhost:7682/judgments/settings \
     -H 'content-type: application/json' \
     -d '{"enabled": true, "sites": {"junk": true}}'
   ```

`GET /judgments/settings` reports what is configured and whether the breaker is
open. `GET /judgments/decisions?site=junk&limit=50` reads the decision log.

## Sites

| Site | Replaces | Effect when it answers |
|---|---|---|
| `junk` | the phrase blocklist in `hallucinations.py` | a transcript is discarded or kept |
| `errors` | nothing — runs only where `error_classify` returned `unknown` | names the failure so the dispatcher can retry, surface, or fail over |
| `naming` | the exact/fuzzy name matchers in `routing.py` | picks the addressed agent, or nobody |
| `router` | the OpenAI routing call in `orchestrator.py` | picks the action and the agent |
| `watch` | reserved; the message watcher is a separate process | not wired yet |

## How "optional" works

Three independent switches, any one of which turns everything off:

1. **No key.** `judge()` returns `None` before any network call.
2. **`judgments.enabled`** — the master switch, default off.
3. **`judgments.<site>`** — per site, default off.

Beyond the switches, `judge()` also returns `None` on a timeout, an HTTP error,
a malformed answer, or while the circuit breaker is open. Callers cannot tell
those cases apart, and they do not need to: all of them mean "use your own
code". Nothing is deleted, so turning a site off restores today's behaviour
exactly.

The call sites all look like this:

```python
answer = judgments.judge("junk", state, questions)
if answer is None:
    return todays_code(...)        # unchanged
return answer.noul("junk") >= JUNK_MIN
```

## Where the thresholds live

`judgments.py` owns transport, credentials, the timeout, the breaker and the
log. `judgment_sites.py` owns the questions and the thresholds.

They are deliberately not one shared number. A confidence figure is only
meaningful next to the cost of being wrong, and those costs differ: discarding
speech loses something the user said, a false `usage_limit` can switch accounts
on its own, picking the wrong agent sends work to the wrong place. So the junk
filter needs 0.60 before it throws anything away, `usage_limit` needs 0.70 while
the other error kinds need 0.50, and naming needs 0.60 before it overrides the
caller's default.

## The decision log

Every call writes a row to `judgment_decisions`: site, question ids, the answer,
latency, tokens, and whether the caller fell back. That is what makes it
possible to review real traffic later without running two engines side by side.

## Latency

The connection is pooled and held open. That matters more than it sounds: a
fresh TLS handshake per call measured 620-700 ms against the same endpoint that
answers in about 280 ms warm, which is most of the budget for a live voice site.

The consequence is that the *first* call after an idle period pays the
handshake — measured at 1.4 s — and so exceeds the 900 ms default and falls back.
That is one transcript handled the old way, not a failure, and every call after
it is warm. Raise `timeout_ms` if you would rather wait than fall back.

Background sites pass their own budget and ignore the default: the error site
asks for 5 s because nobody is waiting on it.

## Known limits

- **Norwegian "vent".** The model reads it as English "vent" (speak freely)
  rather than "wait", at high confidence. That is why the herald floor-intent
  site is not wired. Test Norwegian phrasings before enabling a new site.
- **The same input can move between calls.** Repeated requests have returned
  61%, 81% and 87% for one question. Thresholds need margin; do not tune them to
  the second decimal.
- **Transcripts leave the Host** once a site is on. Off by default is the
  control; there is no redaction step.
- **One vendor, no SLA.** The breaker and the fallback are load-bearing, not
  decoration.
- **The first call after an idle period is slow.** The HTTP connection is
  pooled, which takes a measured median of 278 ms per judgment; a cold TLS
  handshake costs about 1.4 s and will exceed the default 900 ms timeout. That
  call falls back to the site's own code, which is the designed behaviour rather
  than a failure, and the ones after it are warm.
