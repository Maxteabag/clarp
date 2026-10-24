# 0020 — Fading history beneath the items

Status: Accepted. Owner request, 2026-09-11.
Refines temporal weighting and compositing from ADRs 0015–0019.

Use up to one hour of available recorded activity. Recent event weight decays
with a three-minute time constant. Add a modest history term for activity spread
across time: each active minute earns at most one credit, decayed over fifteen
minutes, multiplied by a smooth factor based on the observed time span. A burst
at one instant earns no sustained-history bonus. Repeated activity retains warmth
longer, but all contributions decay and expire. No indefinite accumulation or
inferred activity is permitted; smaller loaded windows provide less history.

Keep world-space sigma and pixel dimensions fixed. Place the main heat field
above the backdrop but below the renderer's items. Updated renderers explicitly
return a transparent background when requested. The host composites backdrop,
heat, then foreground, and adds only 6% of the heat layer over the foreground
(less than 3% opacity for an opaque item with the current palette). This preserves
text, icons and artifact imagery. Older renderers without the transparency flag
receive only the faint overlay until updated; do not mistake opaque frames for
transparent ones. All three maintained map views support the flag.

Test equal-count/equal-last-event bursts against activity spread across minutes,
time decay, duplicate polling and replay bounds. Verify actual pixels prove
strong background heat and at most a faint change to opaque foreground pixels.
