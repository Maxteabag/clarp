# Stage two storyboard — one real piece of work, without captions

Working design aid for the stage-two implementation (Mappy, 2026-09-06). It
records what the observer should notice in the real replay and what the evidence
actually supports. Nothing here is invented progress; everything is recorded.

## The story: Axel builds the pistol feel lab (stickline)

Plan `fdb1454b00daed7d:pistol-six:5898f8c6`, 04:33–04:41. Replay with
`/viz?view=flow&window=1800&until=1788662500000`, action labels hidden.

| Moment | Recorded evidence | What the picture must say | Known / attributed / missing |
| --- | --- | --- | --- |
| Intent | Plan created with four declared items | A new, unlit work object appears in stickline with its own seal | Known: the agent's declaration. Not measured progress. |
| Making | Five file changes (`pistol-attempts.js`, `pistol-lab.js`, `package.json`, `pistol-feel-lab.md`, …) | Panes fill in as material is touched; a soft working glow while a change is running | Attributed by same agent inside the plan window; labeled as such. |
| Checking | `npm run test:pistol-attempts && npm run test:mechanical && npm run build` | A sweep of light around the base while the check runs | Known start and end. |
| Interrupted | That chain exits non-zero | A pane cracks; the glow dims to an ember. The crack stays. | Interruption is certain; the failing step is unknown (&& chain). Say so. |
| Repaired | `npm run test:mechanical && npm run build` and later `npm run test:pistol-attempts` succeed | The crack is mended with a bright seam that stays as a memory | Recovery is exact: every command of the failed chain later passed. |
| Published | Video “Pistol feel lab · six approaches”, thumbnail asset 1440×1250 | The object lights fully and the real picture shows through its front pane; a play mark; Open video opens the recorded media | Known: artifact by the same session inside the window; preview is the recorded thumbnail. |
| Settling | Plan completed 04:41 | The lit object dims slowly over an hour and stays where it stood | Age is known; nothing loops. |

## The honest counterexample: Axel repairs gait transfer (movement-physics)

Plan `fdb1454b00daed7d:movement-physics:4f281400`, 02:41–02:45. Two chains
fail (`npm run build && npm test`, `npm run test:physics && npm test && npm run
test:gameplay-motion`); the later success covers `test:physics` and `npm test`
only. A video is published anyway. The picture must show a lit object with its
real preview **and an unmended crack**, and inspection must list the unresolved
commands (`npm run build`, `npm run test:gameplay-motion`). A published result
does not erase an unresolved check.

## Unknown-evidence cases to exercise

- Intent only, no material yet: unlit wire frame, hollow.
- Plan closed without an artifact: dark panes, no light, a small hollow mark.
- Artifact without a plan: lit object with a hollow tag instead of a seal.
- Agent with no located working directory: object stands in “Location unknown”.
- Preview absent or unsupported: type glyph on the front pane, never a fake image.

## Treatments explored

1. **Slate** (stage one): a hand-cut tablet with a stage rail. Honest but reads as
   a small generic card at overview scale; state lives in tiny cells.
2. **Medallion**: a coin whose rings grow with stages and whose face carries the
   preview. Strong identity, but rings suggest measured progress and the circle
   fights the portraits for attention.
3. **Lantern** (chosen): the product of The Lantern Works. Intent is an unlit
   wire frame; making adds paper panes; checking sweeps light around the base;
   an interruption cracks a pane and dims the light to an ember; recovery mends
   the crack with a gold seam that stays; publication lights the lantern and the
   real preview shows through the front pane. Completed lanterns dim slowly and
   stay standing. Light means state, not decoration: only running work flickers.

The lantern keeps the world's own metaphor, reads as one object from far away
(a lit or dark silhouette) and reveals its material up close. The seal remains
the identity tag hanging from the finial, so the same seal can travel on an
explicit transfer or be pinned at a reference knot, exactly as stage one
established.
