---
name: clarp-ios-crash-diagnostics
description: Diagnose Clarp iOS crashes, UI slowness and API decoding errors such as data is missing. Use server diagnostics and Linux Swift core probes without a Mac.
---

# Clarp iOS Crash & Performance Diagnostics

## Data could not be read / data is missing

First locate the failing endpoint and capture its actual authenticated JSON
privately. A successful HTTP 200 does not prove the native DTO can decode it;
this message alone does not prove missing user records or an outdated server.

Build the clarp-ios repo's portable core with its `scripts/test_core.sh`, then run this skill's own decoder:

```bash
python3 skills/clarp-ios-crash-diagnostics/scripts/decode_core_payload.py /private/response.json \
  --repo /path/to/clarp-ios --type JanitorListResponse
```

Put the repository's Swift toolchain on PATH, or pass `--swiftc`. The helper
links the already-built core objects and prints the actual decoding error and
coding path without printing the payload. It uses the default JSONDecoder;
reproduce custom decoding strategies separately when an API uses them.
This verifies data contracts, not rendered UI.

Swift stored-property defaults do not supply missing keys for synthesized
Decodable. Test independently produced wire JSON, especially empty/partial
objects; encoding a native fixture before decoding it can hide this defect.
Keep explicit restrictions and reject wrong value types. When installed clients
require fields, normalize the public response first so they recover immediately;
avoid changing stored configuration or in-flight generation identity just to
repair serialization. Verify both old-client decoding of the fixed wire shape
and new-client decoding of the captured legacy shape.

The Clarp iOS app reports its own failures. MetricKit crash payloads arrive over
the network at `POST /crash`, and in-app instrumentation streams timing spans to
`POST /clog`. Everything needed to diagnose a crash or a slow screen is already
on the server — no device, no cable, and no Mac.

## Where the data lives

| What | Where |
|---|---|
| MetricKit diagnostic payloads | `~/.local/share/clarp-host/data/crashes/ios-diagnostic-<millis>-<hex>.json` |
| Timing spans, lifecycle, errors (client events) | `client_events` in `~/.local/share/clarp-host/data/client-events.sqlite` (columns `received_at`, `device`, `client_build`, `event`, `level`, `detail`); or `clarp-admin client-log --device D --since 1h --event E` |
| Server-side errors | the Rust Host journal: `journalctl --user -u clarp-host.service` |
| dSYM store for symbolication | `~/.local/share/clarp-dsyms/<run-id>/` |

The Rust Host keeps client events in `~/.local/share/clarp-host/data/client-events.sqlite`, table `client_events`
(`received_at` in ms, `device`, `client_build`, `event`, `level`, `detail`).
Do not open a missing path with sqlite3 and accidentally create an empty DB.

It is shared with the live Host. Read it only (`-readonly`), always scope queries
by time, and never write to it. `clarp-admin client-log` reads the same store.

```bash
sqlite3 -readonly ~/.local/share/clarp-host/data/client-events.sqlite \
  "SELECT datetime(received_at/1000,'unixepoch','localtime'), event, level, substr(detail,1,160)
     FROM client_events
    WHERE received_at >= (strftime('%s','now')-1800)*1000
      AND event LIKE 'ios.%'
    ORDER BY received_at DESC LIMIT 40;"
```

Scope by `received_at`: the store has indexes on `received_at` and on `(device, received_at)`. Do not create indexes or mutate the live database.

Give Python SQLite reads a wall-clock progress-handler budget and save whether
the query completed. An interrupted query with zero returned rows is incomplete,
not an empty window or evidence of recovery. Retain it and inspect the plan;
avoid an automatic retry loop. Query latency is diagnostic-tool cost, not phone
FPS. Apply the existing time/profile/privacy boundaries to the indexed query.

Note that `received_at` is epoch milliseconds (UTC) while the crash file names
and `ls` timestamps are local. Mixing them up makes a crash look like it happened hours from the
evidence that explains it.

## Count canonical timing events, not anomaly copies

`ios.performance.automatic-anomaly` can repeat the original operation fields.
Matching only `operation=conversation-open` in message text therefore duplicates
some timings and overweights slow opens. Filter by event type before parsing:

- Conversation-open samples: `event = 'ios.performance.chat-window-operation'`,
  then `operation=conversation-open`; group by build, client and initial source.
- Emitted slow snapshot samples: `event = 'ios.performance.chat-window-slow-operation'`,
  then `operation=virtualized-snapshot`.
- Keep automatic-anomaly summaries for diagnosis, not as additional samples.

When correcting a report, retain its raw evidence, recompute from canonical events,
mark superseded counts/medians, and recheck earlier baselines rather than assuming
they used the same filter. Moving query windows and null client IDs still prevent
controlled paired comparisons. Thresholded snapshot events are not FPS samples.

## Reading a crash payload

Payloads were once named `crash-<millis>.json`; a later app change renamed them
to `ios-diagnostic-*` and widened the endpoint to carry hangs, CPU exceptions,
disk-write exceptions and slow launches as well as crashes. Both names can exist
on disk; match on both when listing, or a recent crash will look like it never arrived.

```bash
ls -lat ~/.local/share/clarp-host/data/crashes/ | head
python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
for c in d.get('crashDiagnostics') or []:
    print(c.get('diagnosticMetaData'))
" "$(ls -t ~/.local/share/clarp-host/data/crashes/*.json | head -1)"
```

Decode the metadata before theorising:

- `exceptionType 6` / `exceptionCode 1` / `signal 5` is `EXC_BREAKPOINT`
  (`SIGTRAP`) — a **Swift runtime trap**, not memory corruption. Integer
  overflow or conversion, force unwrap of nil, failed precondition, array bounds.
- `exceptionType 1` / `signal 11` is `EXC_BAD_ACCESS` — bad memory access.
- A `JetsamEvent` on the device instead of a crash means the app was killed for
  memory, which is a different disease entirely.

Two fields per frame matter, and only one of them is stable:

- `offsetIntoBinaryTextSegment` is the offset from the `__TEXT` vmaddr, which is
  `0x100000000` for arm64 iOS executables. **This is the reliable coordinate.**
- `address` includes the ASLR slide, so the same frame in two crashes of the
  same build shows different addresses. Never compare addresses across launches;
  compare offsets.

If two payloads carry the same `binaryUUID` and the same app-frame offset, they
are the same bug, not a coincidence. That is the fastest way to tell a
reproducible crash from a one-off.

## Symbolicating the stack

A stack of offsets becomes function names and line numbers only if the dSYM for
that exact `binaryUUID` still exists. TestFlight builds send a copy to Apple via
`uploadSymbols`, which is unreachable from Linux, so the workflow keeps its own
copy as the `clarp-dsyms-<run>` artifact.

```bash
# what UUIDs does this payload need?
scripts/symbolicate_crash.py --latest --list-uuids

# pull the dSYMs for a build into the local store
scripts/fetch_dsyms.sh <workflow-run-id>

# symbolicate
scripts/symbolicate_crash.py --latest
```

Both scripts live in this repo under `scripts/` (`symbolicate_crash.py`, `fetch_dsyms.sh`). They need the
`llvm` package for `llvm-symbolizer` and `llvm-dwarfdump`; `atos` is a Mac tool
and is not required. Frames from Apple binaries (SwiftUI, SwiftUICore,
AttributeGraph) stay unsymbolicated and print as name plus offset, which is
usually enough — the app frame is the one that matters and the Apple frames only
need to show which subsystem was calling.

A locally built dSYM will not match a CI build. `xtool dev build` produces its
own UUID, so check with `--list-uuids` rather than assuming. dSYM archiving
started partway through the app's history (in clarp-ios); builds before that cannot be symbolicated at all,
and for those the best available answer is to disassemble the stripped binary
from the `clarp-ipa` artifact at the crash offset.

To symbolicate a single offset by hand:

```bash
DWARF=<dsym>/Contents/Resources/DWARF/ClarpNative
printf '0x%x\n' $((0x100000000 + <offset>)) |
  llvm-symbolizer --obj="$DWARF" --demangle --functions=short --inlines
```

## Crash loops eat their own evidence

When the app crashes repeatedly, its uploads get cut off mid-body and the server
rejects them. Those lost payloads mean **the crash count on disk is a floor, not
a measurement**:

```bash
journalctl --user -u clarp-host.service --since -2h --no-pager | grep -iE "crash|clog|parse|json" | tail -20
```

A burst of truncated `/crash` and `/clog` bodies is itself a signal — it means
the app is dying faster than it can report.

## Diagnosing slowness

The chat window is instrumented. Start with the summary spans, then let the
slow-operation rows name the culprit rather than guessing:

```bash
sqlite3 -readonly ~/.local/share/clarp-host/data/client-events.sqlite \
  "SELECT datetime(received_at/1000,'unixepoch','localtime'), substr(detail,1,200) FROM client_events
    WHERE event='ios.performance.chat-window-summary'
      AND received_at >= (strftime('%s','now')-3600)*1000
    ORDER BY received_at DESC LIMIT 10;"

sqlite3 -readonly ~/.local/share/clarp-host/data/client-events.sqlite \
  "SELECT datetime(received_at/1000,'unixepoch','localtime'), substr(detail,1,200) FROM client_events
    WHERE event='ios.performance.chat-window-slow-operation'
      AND received_at >= (strftime('%s','now')-3600)*1000
    ORDER BY received_at DESC LIMIT 20;"
```

**`window_ms` is not chat-open latency.** The collector flushes every two
seconds, so a cluster around 2000 is the flush interval, and a huge outlier
means the flush task was delayed (usually app suspension), not that one
operation ran that long. Do not use it as a before-and-after number. The valid
measurements are `transcript-cache:n`, `transcript-cache:t`, the slow-operation
count and maximum, and the FPS/hitch windows. Newer clients emit
`operation=conversation-open` in `ios.performance.chat-window-operation`.
Inspect the matching release source before interpreting it: the current span
finishes after laid-out content, and network opens also wait for transcript
application. Split by build, `initial` (memory/cache/network), `result`, and
navigation settings; do not compare differently configured cohorts as a
controlled before/after. Older builds may lack this span.

Repeated identical spans within the same second usually mean redundant
recomputation rather than one slow algorithm — count the passes per window
before optimising anything. Watch for fan-out upstream of the thing that looks
slow: several observers each requesting the same full snapshot, an invalidation
that ignores known revisions, or a store that rewrites every entry when one
changed.

### Probe bounded transcript warm-up for cache churn

Seed more routes than the idle transcript cache can retain. Warm the recent
routes, settle persistence, clear the transport request recorder, and repeat
unchanged warm-up passes. Count actual `/log` requests and distinct routes;
retain timings as supporting evidence rather than a busy-Mac threshold assertion.
Filtering missing/stale caches before taking the recent-route limit can walk
farther through the fleet on every refresh and continually evict prior warm-ups.
Bound the recent hot set before checking cache eligibility. Verify that a real
change to an older route still synchronizes and that reopening restores history.

For repeated selectable-text sizing, instrument actual UIKit measurements rather
than only the wrapper's callback count. Use the same text and width for paired
runs, then verify changed width, streaming content and font size produce new
heights and preserve selection. Cached layout must be local to the mounted view;
source or simulator improvement does not establish recovered phone FPS.

## Use actual FPS evidence before choosing a hot path

Collect timestamped frame samples without exporting message bodies:

```bash
python3 scripts/collect_ios_frame_summary.py --minutes 30 --build BUILD \
  --output /private/evidence/new-frame-samples.json
```

Read the per-screen sample medians and worst frame times; they are sampled
windows, not a device-wide average. The telemetry policy emits anomalous windows more
often than normal windows; aggregate emitted-sample medians and severe counts
must not be described as time-weighted FPS or percentage of time degraded.
Report sample count, capture range and screen alongside the measurements.
Match the installed build to its exact
release source. Severe FPS across Settings, Teams and Chats points at shared
root work, not just a conversation renderer. Keep system load, server latency,
animation completion and main-thread execution separate.

A `virtualized-snapshot` span ends in UIKit's asynchronous apply completion and
can include animation time. Do not call its entire duration a synchronous UI
stall. Likewise, older Host `httpRequest` timers start before reading the next
keep-alive request; verify the timing boundary and fresh round-trip probes before
claiming a long server handler. An idle timeout is not processing latency.

Inspect root badge and diagnostic context getters: `needsAttentionCount` may
construct the entire inbox, repeatedly sort `AgentStore.chatRoutes` inside job
loops, and repeatedly load archive dictionaries. Measure a fleet-sized native
fixture using the actual AppModel, preserve the correct count, and compare the
same baseline/candidate. Precompute per-pass lookups and avoid constructing
review/working rows for an attention-only count. Test archive and decision
semantics, not just elapsed time. For chat update bursts, verify no overlapping
UIKit snapshot applications, retain the newest complete rows, and preserve user
scrolling. A green simulator run does not establish recovered FPS on the phone;
check new-build device samples after authorized delivery.

## Traps that have actually bitten this app

- **Watchdog termination from reentering a route lock.** Signal 9 with
  `0x8BADF00D`, a scene-update watchdog reason and near-zero application CPU
  can be a deadlock, not CPU overload. Print enough of the attributed stack
  (`--max-frames 100`): an outer `LocalHostRoute.learn` / `withLock` and an
  inner `baseURL` / `withLock`, separated by Foundation and SwiftUI layout,
  identify same-thread reentry. `UserDefaults.set` under the route's NSLock
  can synchronously notify UI observers that read that route again. Capture
  immutable state and enqueue ordered persistence while mutating; perform
  encoding/defaults writes outside the route lock, with no synchronous wait
  on that queue. Skip unchanged writes. Match exact dSYMs and distinguish
  this watchdog signature from a SwiftUI SIGABRT; a source repair is not yet
  proof that all device crashes are resolved.

- **Converting a non-finite `CGFloat` to `Int`.** SwiftUI legitimately proposes
  `.infinity` as a width in `sizeThatFits`, and `Int(infinity)` traps. Clamp with
  `isFinite` before any integer conversion, including inside diagnostic
  arguments — an argument is evaluated before the function it is passed to, so
  instrumentation can crash the very path it measures.
- **A MainActor closure invoked off-main by a system completion handler.** Shows
  up as `EXC_BREAKPOINT` only in Release/TestFlight. The fix is `nonisolated`,
  and it should not be applied broadly on suspicion.

Distinguish these by the frames above the app frame: a concurrency assertion has
dispatch or Swift Concurrency frames, while a layout trap has SwiftUI and
SwiftUICore frames all the way up.

## Working rules

A shared checkout of this repo may have other live agents in it. Do not run `git checkout`, `reset`, or `stash` there;
build and commit in a separate worktree, and verify the current branch before
committing. Never edit the deployed tree under `~/.local/share/clarp-host/` directly.

Pulling raw `.ips` files off the phone over USB with `idevicecrashreport` is a
fallback, not the normal route — the payloads already arrive over the network,
and USB adds nothing that a matching dSYM does not provide. If a USB pull is
genuinely needed, `usbmuxd` is `static` on this host and must be started
explicitly, and the device must appear as vendor `05ac` in
`/sys/bus/usb/devices/` before `idevice_id -l` will see it.

## Slow agent search and New Session

Before attributing a regression to a push, match recent `ios.*` telemetry's
`app_build` to the TestFlight workflow SHA and compare fresh `origin/main`, PR
merge state and the installed build. A separate open header PR is not a fix
removed from the released binary. The shared checkout may be behind main; use
an isolated worktree at the verified ref for source diagnosis and experiments.

For chat search, inspect whether a SwiftUI computed property scans cached
transcripts, how many times the view reads it per render, and whether snippet
normalization runs on MainActor. A native fleet-cache probe should schedule a
MainActor input task before awaiting search and assert it runs before search
finishes. `await` on a synchronous method does not yield. Copy value-only cached
data on MainActor, perform scans in a cancellable worker, debounce message
search while allowing immediate contact-name filtering, and fence displayed
results by query/scope/route membership. Activity-only route reordering should
not continually cancel an in-flight search. Never log search text.

For New Session, measure actual UIHostingController presentation with roughly
100 contacts/routes. Seed Contact definitions as well as active agents: a
simulated server can advertise 100 routes but only the default 10 personas.
Look for section getters repeatedly calling `sessionRoutes(forName:)`, which
sorts the entire fleet, plus repeated Host-target computation. Build a single
per-render projection and pass routes into rows; preserve archive, multi-Host,
case-insensitive name matching and exact-session selection semantics. Keep
projection code pure rather than appending observable diagnostics inside body.

Use native `SearchAndContactPerformanceTests` and `SearchAndContactUITests`
when present, plus portable `ChatSearchExecutionTests`. Retain baseline and
candidate attachment metrics, main-thread responsiveness, ordinary/large-text
search, Back and New Contact form screenshots. Follow ios-mac-dev-loop for
Mac verification before pushing; synthetic timing is not physical-phone FPS.

At accessibility XXXL, a lazy contact List may not instantiate the matching row
until it scrolls into view. Assert its actual accessible name/status from the
retained hierarchy, scroll within the visible area above the keyboard, and
check both existence and hittability. Scroll back to New Contact before tapping
it. A missing exact-label match or an offscreen row is not a measured app stall.
