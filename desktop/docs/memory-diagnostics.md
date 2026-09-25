# Desktop memory diagnostics

The `clarp-desktop` launchers run the app in a systemd scope capped at 3 GiB
(`clarp-desktop-guard`). On 2026-09-25 a preview window (`--no-new-agent`,
build e51d9163, three panes on busy chats) grew from about 240 MB to 3 GiB in
under twelve minutes and was OOM-killed; a second window of the same build
stayed at 240 MB all day. The kernel reported 2.85 GB of *inactive* anonymous
memory, which is the signature of retained heap rather than a burst of work.

## What the process logs now

Once a minute the app logs one line to stderr (the journal under the launcher
scope) that looks like:

```
memory {"RssAnon":140268,"RssFile":77884,"VmHWM":219868,"VmRSS":218152,"VmSwap":0,
        "agents":41,"avatarRequests":0,"avatarSources":31,"contactAvatarSources":5,
        "conversationRows":416,"conversations":4,"items":4550,"logRequestsInFlight":1,
        "narratorCache":0,"textItems":27}
```

Kernel fields are kilobytes from `/proc/self/status`. `items` and `textItems`
count live Qt Quick items under the window (all items, and text/text-edit
items); `conversations`, `conversationRows`, `avatarSources`,
`contactAvatarSources`, `avatarRequests`, `narratorCache` and
`logRequestsInFlight` are the controller's own containers.
`CLARP_MEMORY_LOG_SECONDS` changes the cadence; `0` disables it.

How to read a run that ends in an OOM kill:

```bash
journalctl --user -t clarp-desktop --since '-2h' | grep ' memory {'
```

- `RssAnon` climbing while `items`/`textItems` climb: leaked delegates
  (ListView rows, tool cards, popups). Look at what the visible panes show.
- `RssAnon` climbing with flat item counts: non-item allocations, such as
  decoded images, retained network buffers or model rows. Check
  `conversationRows` and the avatar/media counters.
- Flat `RssAnon` with high `VmHWM`: a spike that was freed; not a leak.

## What has been ruled out

Headless labs with the same binary against the live Host stayed flat for four
to five minutes in all of these configurations (see
`~/dotfiles/skills/qt-clarp-desktop/scripts/clarp-desktop-memlab.sh`):

| Lab | Renderer | Panes | Narration | Result |
| --- | --- | --- | --- | --- |
| A | software | 1 busy chat | off | 199–204 MB flat |
| B | software | 1 busy chat | Plain English, group old, Paper | 194–200 MB flat |
| C | OpenGL (Mesa), Kvantum | 3 busy chats | Plain English, group old, Paper | 379–386 MB flat |

Lab C generated the same traffic shape as the killed window: hundreds of
`/log` delta polls per minute across three sessions and about 80 tool
explanation requests per minute, so the polling and narration paths on their
own do not leak under offscreen rendering.

## What is known about the traffic

Every `transcript-updated` event triggers a `/log?after_revision=` delta for
that session, gated to one in flight plus one pending per session. During a
streaming turn that reaches several polls per second per pane (700 polls in
three minutes for one session was observed). It is bounded, but it is wasted
Host and client work; coalescing deltas per session to roughly 150–250 ms
would cut it by an order of magnitude without visible latency. That is a
separate change from the leak and is not made here.

## What still needs a real occurrence

The remaining differences between the labs and the killed window are the
visible Wayland surface (continuous frame presentation, `uiScale` 1.15 layer
scaling) and user interaction (scrolling through older history, expanding tool
groups, hovering links, text selection). The memory log above exists so the
next kill leaves a trend in the journal instead of only the kernel's final
line; compare the last few `memory` lines before the OOM entry with a healthy
window's lines.
