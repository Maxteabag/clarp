# Maximum readability for the Clarp desktop app, and themes to match

Branch `slint-readability-research` (from origin/main 85a54ab4). Not pushed to main: Peter picks the themes first.
Screenshots are in `/var/tmp/slint-readability-shots/`. The rules are enforced by
`desktop-rs/core/tests/readability.rs` (`cargo test -p clarp-core`), and
`desktop-rs/tools/theme-audit.sh` prints the full audit.

## TL;DR

- **Five new themes, all passing the rules:** **Sepia** (Atkinson Hyperlegible on warm sepia, for day),
  **Graphite** (low-glare dark: off-white on warm charcoal), **Night** (a serif in parchment tones on
  brown-black), **High contrast** (Atkinson 18px, near-black on off-white, for glare and LoDPI screens) and
  **Studio** (Noto Sans 15px on neutral grey, for dense chrome).
- **All four current themes fail.** Terminal, Dusk and Hyperlegible each fail 34 checks; Paper fails 17.
  The worst problems:
  - Tokyo Night's success green and warning orange are **identical to a deuteranope** (CIEDE2000 ΔE 0.9).
    Paper's success and danger are 2.5 apart for the same viewer.
  - Muted text in Terminal and Dusk is only **APCA Lc 45**, and in Hyperlegible Lc 57. Muted text covers
    quotes, tool summaries, key-hint labels and pane titles.
  - The **error banner** in the dark themes is Lc 46.
  - **Paper's links fail WCAG AA** (4.19:1 on the page, 4.05:1 in a bubble).
  - Faint text and the 1px borders are too faint in every dark theme.
- **Layout problems turned up by the screenshots.** I fixed two and found one more:
  - Fixed: paragraphs inside a reply had **no gap** between them.
  - Fixed: the theme's `measure` was **never applied**, so lines ran about 105 characters at 1280px, and
    more on an external monitor.
  - Not fixed: a **quote that wraps is measured as one line**, so the text after it overlaps the next
    message. This bug is pre-existing; see §7.
- **Prose line spacing can't be set from the theme yet.** Slint 1.18's `StyledText`, which draws all
  prose, has no `line-height-factor`, so the leading comes from the font itself: Atkinson 1.24em, Noto
  1.36em, JetBrains Mono 1.32em. Peter's "generous line spacing" is therefore a font choice until Slint
  adds it.

## 1. How this app actually renders text (from the code)

I read these facts from `slint 1.18.1`, `i-slint-renderer-software` and `swash` in `~/.cargo/registry`.
They decide which research applies here.

| Fact | Source | Consequence |
|---|---|---|
| Glyphs are rasterised by swash: `Source::Outline`, `Format::Alpha`, with no `.hint()` call | `i-slint-renderer-software-1.18.1/fonts/vectorfont.rs:207-210` | Antialiasing is greyscale only: no subpixel/ClearType and no hinting. On a LoDPI external monitor small text looks soft, so size matters more than it would under FreeType with hinting. |
| Glyphs are placed at quarter-pixel horizontal positions (`SUBPIXEL_BIN_COUNT = 4`) | same file, line 29 | Letter spacing stays even. This is subpixel positioning, not subpixel AA. |
| Coverage is blended straight into 8-bit sRGB, with no linear-light step | `draw_functions.rs` (digest §8) | FreeType's documentation says naive blending makes "black-on-white heavier than white-on-black" ([FreeType](https://freetype.org/freetype2/docs/hinting/text-rendering-general.html)). Light text on dark renders thin, so dark themes need more contrast and size, not less. Dark text on sepia gets a little free weight. |
| `Text` and `TextInput` have `line-height-factor` and `letter-spacing`; **`StyledText` has neither** | `i-slint-compiler-1.18.1/builtin_elements.rs:1105-1117, 1191-1212` | Prose, headings, quotes and table cells are all `StyledText`, so their line height is the font's natural height. Code blocks are `Text` and could take a factor. |
| The app's chrome font is hard-coded to JetBrains Mono at `Palette.body-size`; the transcript uses the theme font | `slint-app/ui/app.slint:85` | The explorer, key hints and pane titles stay monospace whatever the theme. 106 places use 10–11px text. |
| Interface scale is 1.15 on a real display and 1.0 in the checks | `slint-app/src/main.rs:581` | On a real screen every px value below is 15% larger. |

The fonts' natural line heights, read from their hhea/OS/2 tables, set the prose leading:

| Font (installed) | Line height (em) | x-height (em) |
|---|---:|---:|
| Atkinson Hyperlegible | 1.24 | 0.496 |
| iA Writer Quattro S | 1.30 | 0.516 |
| JetBrains Mono | 1.32 | 0.550 |
| Noto Sans / Noto Serif | 1.36 | 0.536 |
| Liberation Serif | 1.15 | 0.459 |

## 2. Evidence (primary sources), and how strong it is

**Contrast metrics**
- WCAG 2.2 sets 4.5:1 for normal text and 3:1 for large text (AA), and 7:1 / 4.5:1 for AAA. Non-text UI
  and focus indicators need 3:1 (1.4.11). Use of colour (1.4.1) says colour must not be the only signal.
  [Understanding 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html),
  [1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html),
  [WCAG 2.2](https://www.w3.org/TR/WCAG22/).
  *Strong as a compliance floor; contested as a model of perception.*
- The WCAG ratio overrates light-on-dark pairs ([w3c/wcag#695](https://github.com/w3c/wcag/issues/695);
  independent critique: [xi/apca-introduction](https://github.com/xi/apca-introduction)). This shows up
  directly in our audit: Terminal's `#c0caf5` chrome text on `#1a1b26` passes WCAG at **10.6:1** but scores
  only **APCA Lc 74**.
- APCA-W3 0.0.98G-4g ([constants](https://github.com/Myndex/apca-w3/blob/master/src/apca-w3.js),
  [levels](https://github.com/Myndex/SAPC-APCA/blob/master/documentation/APCAeasyIntro.md),
  [size/weight pairs](https://github.com/Myndex/SAPC-APCA/blob/master/documentation/APCA_in_a_Nutshell.md)):
  - **Lc 90** is preferred for body text (14px/400).
  - **Lc 75** is the body-text minimum (18px/400, 16px/500, 14px/700).
  - **Lc 60** is for other content text (24px/400, 16px/700).
  - **Lc 45** is for headlines.
  - **Lc 30** is the absolute floor for any text.
  - **Lc 15** is for discernible non-text.
  - The dark-mode maximum is about Lc −90 for large text (preliminary).

  The WCAG 3 draft of 10 Sep 2026 says its contrast algorithm is "yet to be determined" and does not name
  APCA ([WCAG 3.0](https://www.w3.org/TR/wcag-3.0/)). *Weak to contested.* So **this app requires both**:
  APCA for perception, especially in dark mode, and WCAG 2 as the floor.

**Polarity (light vs dark)**
- Dark-on-light gives better proofreading and acuity. The advantage holds independent of ambient light
  ([Buchner & Baumgartner 2007](https://doi.org/10.1080/00140130701306413)) and across ages
  ([Piepenbrock et al. 2013](https://doi.org/10.1080/00140139.2013.790485)). It **grows as text gets
  smaller** ([Piepenbrock et al. 2014a](https://doi.org/10.1177/0018720813515509)), and fits the
  smaller-pupil, sharper-image explanation ([2014b](https://doi.org/10.1080/00140139.2014.948496)).
- Glance legibility is worst for dark mode in a dark room
  ([Dobres et al. 2017](https://doi.org/10.1016/j.apergo.2016.11.001)).
- Some readers with cloudy ocular media read light-on-dark faster
  ([Rubin & Legge 1989](https://doi.org/10.1016/0042-6989(89)90175-2)).
- *Strong for normal vision on short tasks; nobody has tested hours of reading.* Halation or blooming in
  dark mode for astigmatic eyes has **no primary source**; I found none, so the claim is unverified.
- **For this app:** make the light themes the default for long day reading (Sepia, High contrast). The
  dark themes are comfort options and need more Lc, not less, because light text also renders thinner
  here (§1).

**Sepia, tints, off-white, pure black**
- Coloured overlays perform at placebo level, per a systematic review
  ([Griffiths et al. 2016](https://doi.org/10.1111/opo.12316)); a rebuttal is
  [Evans et al. 2017](https://doi.org/10.1016/j.optom.2016.08.002).
- I found **no controlled study of sepia**, off-white or pure black. Off-white mostly just lowers
  luminance (the same as dimming). OLED "black smear" is a pixel-response artefact, not a reading result.
  *Weak:* sepia is a legitimate comfort preference **as long as luminance contrast stays high**, which is
  what the rules enforce.
- Choices made here: no theme uses `#000`/`#fff` text or backgrounds (existing test: < 18:1). The dark
  backgrounds are L* 7–12, not 0.

**Typefaces**
- **Atkinson Hyperlegible** is designed for letter differentiation (B8, O0, 1Il), with open counters
  ([Braille Institute](https://www.brailleinstitute.org/freefont/)). Its rationale matches
  [Beier & Larson 2010](https://doi.org/10.1075/idj.18.2.03bei), where wider letters and taller x-heights
  helped. *No peer-reviewed evaluation of Atkinson itself (weak).*
- **Lexend** claims a gain from 20 third-graders, not peer-reviewed (weak).
- **Dyslexia fonts:** OpenDyslexic gave no benefit
  ([Wery & Diliberto 2017](https://doi.org/10.1007/s11881-016-0127-1)). Dyslexie gave no benefit either
  ([Kuster et al. 2018](https://doi.org/10.1007/s11881-017-0154-6)), and its 7% gain in another study was
  spacing, not letter shape ([Marinus et al. 2016](https://doi.org/10.1002/dys.1527)). *Strong null.*
- [Rello & Baeza-Yates 2013](https://doi.org/10.1145/2513383.2513447): sans, roman and monospaced faces
  beat serif and italic for readers with dyslexia. *Moderate.*
- A humanist sans beat a square grotesque in glance tasks
  ([Reimer et al. 2014](https://doi.org/10.1080/00140139.2014.940000)).
- Rationale pages for Literata, Source Serif 4 and Inter are design intent, not evidence
  ([Literata](https://www.typetogether.com/literata-font), [Inter](https://rsms.me/inter/)).
- **Print size:** the fluent range is about **0.2°–2° x-height**
  ([Legge & Bigelow 2011](https://doi.org/10.1167/11.5.8)). At 96 DPI and 60cm, 16px with a ~0.5em x-height
  is about 0.2°, the *bottom* of that range. Hence 17px bodies on the proportional themes, 18px for High
  contrast, and no body under 15px. *Strong.*

**Line length, line height, spacing**
- About 55 characters per line gave the best comprehension; around 100 is read faster but understood
  less ([Dyson & Haselgrove 2001](https://doi.org/10.1006/ijhc.2001.0458),
  [Dyson 2004](https://doi.org/10.1080/01449290410001715714),
  [Shaikh & Chaparro 2005](https://doi.org/10.1177/154193120504900514)).
- WCAG 1.4.8 (AAA) asks for ≤ 80 characters, 1.5 line spacing, and paragraph spacing 1.5× the line
  spacing ([Understanding 1.4.8](https://www.w3.org/WAI/WCAG22/Understanding/visual-presentation.html)).
  1.4.12 is a robustness rule, not a default.
- *Moderate* for 50–80 cpl; *weak* (old, thin literature) for any specific line height above 1.2–1.5.
- Extra letter spacing for dyslexia ([Zorzi et al. 2012](https://doi.org/10.1073/pnas.1205566109)) did not
  replicate ([van den Boer & Hakvoort 2015](https://doi.org/10.1080/17470218.2014.964272);
  [Galliussi et al. 2020](https://doi.org/10.1007/s11881-020-00194-x)) and never helps fluent adults.
  *Contested:* letter spacing stays at 0.

**Colour-vision deficiency**
- About 8% of men and 0.4% of women of European descent have a red-green deficiency
  ([Birch 2012](https://doi.org/10.1364/JOSAA.29.000313)).
- Simulation uses the Machado, Oliveira & Fernandes 2009 matrices at severity 1.0 on linear RGB
  ([paper](https://doi.org/10.1109/TVCG.2009.113),
  [matrices](https://www.inf.ufrgs.br/~oliveira/pubs_files/CVD_Simulation/CVD_Simulation.html)). The source
  is ambiguous about linear vs gamma RGB, and DaltonLens rates Viénot/Brettel higher for dichromats
  ([DaltonLens](https://daltonlens.org/opensource-cvd-simulation/)). *Moderate.*
- Okabe–Ito is the reference safe palette ([jfly](https://jfly.uni-koeln.de/color/)), but several of its
  colours fail 4.5:1 as text on light backgrounds, so the status colours here were searched for instead.

**Syntax highlighting**
- No comprehension benefit in 390 students
  ([Hannebauer et al. 2018](https://doi.org/10.1007/s10664-017-9579-0)) against a positive result in 10
  ([Sarkar 2015](https://ppig.org/files/2015-PPIG-26th-Sarkar1.pdf)). *Contested.* Code blocks stay
  single-colour, `text` on `sunken`, which keeps the body contrast; highlighting would be a preference
  feature.

**Focus indicators**
- 2.4.13 (AAA) asks for an indicator at least a 2px perimeter, with 3:1 between focused and unfocused
  states. 1.4.11 asks for 3:1 against adjacent colours
  ([Understanding 2.4.13](https://www.w3.org/WAI/WCAG22/Understanding/focus-appearance.html)).
  *Normative, with no empirical basis for the exact numbers.* The app's `FocusRing` is **1px**, which is
  below 2.4.13. Recommendation: 2px.

## 3. The rules (what the test enforces)

The tiers below are applied to **every foreground and surface pair the Slint UI actually draws**: 48
pairs, listed in `core/src/readability.rs::PAIRS`. That includes the keyboard cursor (`accent` at 16%
over `window` or `raised`) and the field selection (`accent` at 35% over `control`), composited the way
the renderer composites them. Each pair must reach **both** the APCA |Lc| and the WCAG ratio.

| Tier | Roles | APCA Lc | WCAG |
|---|---|---:|---:|
| body | `text` on window, bubble, sunken (code), raised (tool cards, table headers), hover, control, cursor | **75** | **7:1** |
| chrome | `chromeText` (headings, names, explorer titles) on window, raised, control, hover, both cursors | **75** | **7:1** |
| muted | `mutedText` (quotes, tool summaries, key-hint labels, pane titles) on every surface and both cursors | **60** | **4.5:1** |
| faint | `faintText` (timestamps, counts, captions, copy icon); text in a field selection | **45** | **4.5:1** |
| signal | `link`, `accent` (names, active titles, key hints), `accentText` on accent, `warning`/`danger`/`success` text, `danger` on `dangerSurface` (error banner) | **60** | **4.5:1** |
| focus | `accent` (focus ring, active frame) against `control` | **45** | **3:1** |
| outline | `border` on window and raised | **15** | **1.5:1** |

Further rules:
- **Status colours under colour-vision deficiency.** The pairs success/warning, success/danger,
  warning/danger, link/danger and accent/danger must each be **CIEDE2000 ≥ 12** apart under normal vision,
  protanopia, deuteranopia and tritanopia.
- **Body size:** 15–22px. **Measure:** 55–95 characters, at 0.5em per character (0.6 for monospace).
- **Never pure polarity:** text and background are never `#000`/`#fff`, and contrast stays under 18:1
  (the existing test).
- **Disabled text:** none drawn today. If added, it needs Lc 30 (APCA's floor for any text). WCAG exempts it.

Recommended but not enforced, because they need UI changes rather than palette changes:
- Body line height ≥ 1.4em once `StyledText` supports it. Until then prefer fonts with natural height
  ≥ 1.3 for prose.
- Paragraph spacing ≥ 0.5em. Now 8px, about 0.47em at 17px.
- Chrome text ≥ 12px. Key hints and pane titles are 11px, which APCA would want at Lc 90 or more.
- A 2px focus ring.
- A glyph or word next to every status dot (§7).

Why these levels:
- Lc 75 is APCA's body minimum; I chose it over the preferred Lc 90 because the dark themes can't
  reach 90 without glare.
- 7:1 is AAA, which the old test already required for body text.
- Muted and signal text are real content at 11–13px, so they get APCA's content level (60) and WCAG AA.
- Faint is incidental text, but people still read it (timestamps), so it keeps WCAG AA with APCA's
  headline level.

## 4. Audit of the current themes

Computed by `tools/theme-audit.sh` on commit `dc7e52be`, before the fix proposal.

| Theme | Fails | Body text on window | Worst problems |
|---|---:|---|---|
| terminal | 34 | Lc 87.5 / 13.2:1 ok | muted `#8f96bc` Lc 39–46; faint Lc 31–37; link Lc 50–51; accent Lc 54–56; danger (error banner) Lc 46–47; chromeText Lc 68–74 (WCAG says 7.3–10.6); border Lc 8; **success and warning ΔE 0.9 under deuteranopia (identical)**, 5.5 under protanopia; warning and danger ΔE 9.0 under tritanopia |
| dusk | 34 | Lc 82.6 / 12.2:1 ok | as terminal; muted `#a39a8c` Lc 41–48 |
| hyperlegible | 34 | Lc 90.7 / 13.8:1 ok | as terminal; muted `#a6adc8` Lc 51–58 |
| paper | 17 | Lc 88.2 ok | **link `#1f7a78` 4.19:1 on window, 4.05:1 on bubble (fails WCAG AA)**; faint 4.0–4.46:1 on bubble, sunken and cursor (fails AA); text on the cursor Lc 73.6; under deuteranopia success and danger are ΔE 2.5 apart, warning and danger 6.9; accent and danger ΔE 5.7–11.4 (a rust accent reads like danger to a dichromat) |

Every failing pair is listed in the appendix.

## 5. Proposed themes (new entries in `reading_themes.json`)

Every role is filled. All five pass every rule in §3 (0 failures). The neutral colours were set by hand.
The signal colours (danger, warning, success, link, accent) come from a constrained OKLCH search: each
stays inside its expected hue window (red, amber, green, blue, accent hue) and must pass all the contrast
pairs. Within that, the search maximises the smallest CVD separation up to ΔE 13–14, then lowers chroma
so the theme stays calm.

| Theme | Use | Font chain (resolves on this machine) | Size / measure | Body on window | Lowest muted / faint / signal |
|---|---|---|---|---|---|
| **sepia** | long reading by day | Atkinson Hyperlegible Next → **Atkinson Hyperlegible** → Noto Sans → sans-serif | 17px / 680px (~80 ch) | `#2a2016` on `#f4ecdb`, Lc 92.0 / 13.6:1 | Lc 66 / 58 / 62 |
| **graphite** | low-glare dark, evenings | Atkinson Hyperlegible Next → **Atkinson Hyperlegible** → Noto Sans | 17px / 680px | `#e2ddd4` on `#1c1d20`, Lc 84.6 / 12.5:1 | Lc 62 / 45 / 60 |
| **night** | dim room, warm, little blue | Literata → Source Serif 4 → **Noto Serif** → serif | 17px / 660px (~78 ch) | `#e0d4bd` on `#16130f`, Lc 80.5 / 12.6:1 | Lc 63 / 49 / 60 |
| **contrast** | glare, bright rooms, LoDPI external monitor | Atkinson Hyperlegible Next → **Atkinson Hyperlegible** → Noto Sans | 18px / 700px (~78 ch) | `#141414` on `#fbfaf6`, Lc 102 / 17.6:1 | Lc 78 / 57 / 65 |
| **studio** | dense explorer and chrome, big monitor | **Noto Sans** → Inter → IBM Plex Sans → sans-serif | 15px / 680px (~91 ch) | `#1d1e22` on `#f3f3f0`, Lc 96.3 / 15.0:1 | Lc 66 / 55 / 64 |

**Rationale**
- **Sepia.** Peter's terminal combination (Atkinson + sepia), tuned to the rules.
  - The page is slightly darker and less saturated than Paper's, for less glare.
  - Text is a warm brown-black (`#2a2016`), not black, because naive sRGB blending already thickens dark
    glyphs (§1).
  - The status colours are a teal-green, an ochre and a brick red. They separate by lightness as well as
    hue, so they stay apart for every dichromat (min ΔE 14.6).
  - Caveat: Atkinson's natural leading is the tightest of the installed faces (1.24em). Paragraph gaps (§6)
    compensate. If Peter wants more air before Slint gets line-height, iA Writer Quattro S (installed,
    1.30em) or Noto Sans (1.36em) are drop-in alternatives.
- **Graphite.** A dark theme that is not pure black: L* about 12, slightly warm, against a warm off-white
  `#e2ddd4`.
  - It deliberately stays below the APCA dark-mode ceiling (≈ Lc −90) to avoid glare and halation, while
    clearing Lc 75 on every surface, including the keyboard cursor.
  - Light text renders thin in this renderer (§1), so it gets 17px, not 15–16.
  - Muted and faint are lighter than designers usually pick, because APCA (rightly) penalises mid-grey on
    dark. In dark mode the hierarchy comes from size and weight more than from colour.
- **Night.** For reading in the dark: a brown-black page, parchment-coloured text, an amber cast, and
  pale low-chroma status colours. Its body contrast (Lc 80) is the lowest the rules allow.
  - Noto Serif's 1.36em natural leading makes it the airiest prose of all the themes today.
  - The evidence (Dobres 2017) says dark mode in a dark room is the *worst* for glance legibility. That is
    why Night keeps body contrast above Lc 75 instead of dimming the text further.
  - Literata (Paper/Dusk's intended face) is not installed; with ttf-literata-git it becomes the first
    choice here too.
- **High contrast.** Near-black (`#141414`) on off-white (`#fbfaf6`), Lc 102 and 17.6:1, kept under the
    18:1 polarity cap.
  - Atkinson at 18px, deep status colours, a violet accent kept far from danger, and solid borders
    (outline Lc 69).
  - For sunlight on the Surface Laptop Studio, glare, tired eyes, and the unhinted greyscale text on a
    LoDPI monitor, where the extra size and contrast pay off most.
- **Studio.** For the dense days.
  - Noto Sans at 15px: its 0.536 x-height and 1.36em leading make 15px read like other faces' 16–17px.
  - Neutral grey without a colour cast, so status and accent colours carry meaning against it.
  - ~91 characters per line: Dyson and Shaikh found long lines are read faster, and scanning dominates
    dense work.

**Colour-vision simulation of the status colours (Machado 2009, severity 1.0).** The table gives the
smallest CIEDE2000 distance among success/warning/danger/link/accent-vs-danger under each vision (rule:
≥ 12). The full per-colour simulation is in `tools/theme-audit.sh` output.

| Theme | normal | protanopia | deuteranopia | tritanopia |
|---|---:|---:|---:|---:|
| sepia | 19.5 | 14.6 | 14.6 | 14.6 |
| graphite | 27.8 | 15.1 | 14.1 | 14.2 |
| night | 26.0 | 14.1 | 14.0 | 14.4 |
| contrast | 28.9 | 15.9 | 14.0 | 15.9 |
| studio | 27.5 | 15.5 | 14.0 | 16.7 |
| *terminal today* | — | 5.5 | **0.9** | 9.0 |
| *paper today* | — | 6.7 | **2.5** | 8.7 |

Simulated hexes, sepia under deuteranopia: success `#585856`, warning `#70652a`, danger `#474023`. They
are told apart by lightness (grey, olive, dark olive), which is why these palettes vary lightness as well
as hue. Colour alone is still not enough: see §7.

**Fonts on Arch.** Installed: Atkinson Hyperlegible (`ttf-atkinson-hyperlegible`), Noto Sans/Serif
(`noto-fonts`), JetBrains Mono, iA Writer, Liberation, Roboto, Cascadia/Atkynson Mono Nerd. Not installed,
and each theme falls back cleanly without them:

| Font | Package | Effect |
|---|---|---|
| Atkinson Hyperlegible Next | AUR `ttf-atkinson-hyperlegible-next` (or `-variable`) | First choice in sepia, graphite and contrast: more weights and better metrics |
| Literata | AUR `ttf-literata-git` | Paper, Dusk and Night's first choice. **Paper and Dusk render in Noto Serif today, not Literata.** |
| Source Serif 4 | extra `adobe-source-serif-fonts` | Night's second choice |
| Inter, IBM Plex Sans | extra `inter-font`, `ttf-ibm-plex` | Studio fallbacks. Note Inter's leading is tighter than Noto's. |
| Atkinson Hyperlegible Mono | AUR `ttf-atkinson-hyperlegible-next-mono` | Candidate code face; code is hard-coded to JetBrains Mono today |

Installing Literata or Atkinson Next changes how the themes render (different metrics), so re-run
`tools/theme-shots.sh` afterwards.

## 6. Fixes made beyond the palettes (each its own commit)

1. **`fix(engine)`: each top-level paragraph and list is its own prose block** (`d212af07`). Slint's
   `StyledText::from_markdown` sets Markdown paragraphs one line apart, so paragraphs and lists ran
   together with no visible break (see the first shots). Each now takes the transcript's 8px block
   spacing. Engine tests are updated.
2. **`feat(slint)`: the reading column is the theme's measure wide** (`d7f71c45`). The theme field
   `measure` existed but nothing applied it. The transcript now pads its right side, so a reply is at most
   `measure` px wide (60–80 characters), and user bubbles align to the column's right edge. I used padding
   rather than `max-width` on purpose, because `max-width` gives Slint wrong text heights.
3. **`test(slint)`: a padded row taller than the viewport counts as in view** (`fd6d4f52`). The scroll
   check's geometry ignored the row's 7px top and bottom padding. With the narrower column, the stream
   fixture row grew to 686px; with its padding that is 700px, more than the 688px viewport, so its top
   can't be in view while its bottom is. The list itself really was at the end and following; only the
   check's tolerance was wrong.

## 7. Problems found, not fixed

- **A wrapped quote is measured as one line** (pre-existing on main, in every theme). Its message row ends
  up too short, and the next message is drawn over the quote's last lines. Screenshot:
  `bug-wrapped-quote-overlaps-next-row.png`.
  - I reproduced it on the unchanged `transcript.slint` with a two-line quote, so it is not caused by the
    column change; the narrower column only makes wrapping more frequent.
  - Cause: `HorizontalLayout { bar; StyledText }` measures the text's height at its *preferred*
    (one-line) width. Setting a zero preferred width produced one word per line.
  - Binding the preferred width to the block's width is a binding loop, and a `Rectangle` +
    `VerticalLayout` wrapper was worse.
  - Needs a proper fix, for example building the quote from a `VerticalLayout` with the bar drawn by the
    row, or upstream height-for-width in `HorizontalLayout`. Until then the themes check keeps its sample
    quote to one line.
- **Tool status is shown by colour alone:** a 6px dot (`transcript.slint:83`). That breaks WCAG 1.4.1. Even
  with the new palettes, three dots are hard to tell apart at that size. Add a glyph (✓ ✕ …) or the word
  "failed"/"running".
- **Line height for prose cannot be themed** (§1). When Slint gives `StyledText` `line-height-factor`, add
  a `lineHeight` theme field: about 1.45 for the reading themes, 1.3 for studio. `Text` code blocks could
  take it now.
- **11px chrome text and key hints** (106 places) are below APCA's size guidance, even at Lc 75. The
  palettes give them Lc ≥ 60; going to 12–13px would do more.
- **The focus ring is 1px**; WCAG 2.4.13 asks for 2px.
- **The chrome font is fixed to JetBrains Mono** for every theme, so the proportional themes keep a mono
  explorer. That suits a keyboard-first explorer, but a `chromeFamily` theme field would allow Atkinson
  Mono or Noto Sans there.
- **Unused roles:** `background`, `secondary`, `selection`, `selectedText`, `shadow` and `scrim` are in the
  JSON (legacy of the Qt app) but Slint never draws them. `Palette.window` is the transcript background,
  not `background`; in Paper the two differ.

## 8. Separate proposal: fixes for the current themes (commit `892561c8`)

This is **clearly marked and kept to one commit, so it can be dropped.** Neutral roles only move in
OKLCH lightness, keeping their hue; the status colours are re-picked near their old hues. Without this
commit `every_theme_meets_the_readability_rules` fails on these four themes. The alternatives are to skip
them in the test, or to keep the commit.

| Theme | Changes (old → new) |
|---|---|
| terminal | mutedText `#8f96bc`→`#b5bde4`, faintText `#8085a0`→`#9ea3c0`, chromeText `#c0caf5`→`#cfd7fc`, link `#7aa2f7`→`#92b5fe`, accent `#bb9af7`→`#c5a7fe`, border `#41445a`→`#54586f`, danger `#c98a98`→`#e8a5a3`, warning `#e0af68`→`#f2dda0`, success `#9ece6a`→`#a5f0db` |
| dusk | same as terminal, except mutedText `#a39a8c`→`#c8beb0` and faintText `#8a8378`→`#aaa397` |
| hyperlegible | same as terminal, except mutedText `#a6adc8`→`#b8bfda` and faintText `#8d93b0`→`#9da3c1` |
| paper | text and chromeText `#3d2e20`→`#352618`, mutedText `#6b5b4d`→`#615143`, faintText `#75654f`→`#6d5d47`, link `#1f7a78`→`#117270`, accent `#9c4f24`→`#8b5441`, danger `#9a3434`→`#651f24`, warning `#7a4b00`→`#7b5b28`, success `#3f6b1f`→`#1d5c4c` |

The cost: Tokyo Night's saturated green and orange become a pale mint and a pale gold. That is the price
of telling them apart under deuteranopia without changing their hues. Shots of the fixed versions are in
`/var/tmp/slint-readability-shots/existing-fixed/`.

## 9. Screenshots

`/var/tmp/slint-readability-shots/`:
- `theme-<id>-top.png` (the start of a long reply: heading, prose with a link and inline code, a list, a
  numbered list, a code block) and `theme-<id>-end.png` (table, quote, tool calls with
  completed/error/running dots, a user bubble, the error banner, the explorer and key hints), for all nine
  themes as of the final branch.
- `existing-fixed/` holds the fixed current themes.
- `bug-wrapped-quote-overlaps-next-row.png` shows the bug in §7.

Regenerate with `cargo build -p clarp-slint && tools/theme-shots.sh OUT [THEME...]`. That runs
`check.sh themes` once per theme, so each theme is laid out in its own font. They are 1280×800 at scale
1.0; a real display draws at 1.15.

I looked at every shot. What read badly, and what happened to it:
- Run-together paragraphs and ~105-character lines (fixed, §6).
- The quote overlap (§7).
- The colour-only status dots (§7).

In High contrast and Studio, the explorer's 11px mono chrome next to 18px/15px proportional body text is
the most visible mismatch.

## 10. Commits on the branch

```
fd6d4f52 test(slint): a padded row taller than the viewport counts as in view
892561c8 fix(core): proposed readability fixes for terminal, paper, dusk and hyperlegible   <- proposal
dc7e52be test(slint): a themes check that shoots every reading theme
d7f71c45 feat(slint): the transcript's reading column is the theme's measure wide
d212af07 fix(engine): each top-level paragraph and list is its own prose block
b9643885 feat(core): sepia, graphite, night, high-contrast and studio reading themes
03a7965a test(core): readability rules for the reading themes (failing)
```

Gates, all run on the final branch:
- `cargo build -p clarp-slint` passes (exit 0).
- `cargo test -p clarp-core -p clarp-engine -p clarp-slint` passes (exit 0, 38 suites, no failures).
- Checks `transcript`, `settings`, `composer`, `artifacts` and `updates` give E2E_PASS.
- `scroll` gives E2E_PASS after `fd6d4f52`; before it, two geometry cases failed, as described in §6.

## Appendix: every failing pair in the current themes

From `tools/theme-audit.sh` at `dc7e52be` (rules as committed). Format: role on surface (colours): measured vs needed.

- terminal: chromeText on window (#c0caf5 on #1a1b26) is Lc 73.8 / 10.59:1, chrome needs Lc 75 / 7:1
- terminal: chromeText on raised (#c0caf5 on #20212e) is Lc 73.0 / 9.87:1, chrome needs Lc 75 / 7:1
- terminal: chromeText on control (#c0caf5 on #292b3a) is Lc 71.2 / 8.67:1, chrome needs Lc 75 / 7:1
- terminal: chromeText on hover (#c0caf5 on #22232f) is Lc 72.7 / 9.64:1, chrome needs Lc 75 / 7:1
- terminal: chromeText on accent@0.16/raised (#c0caf5 on #39344e) is Lc 68.2 / 7.33:1, chrome needs Lc 75 / 7:1
- terminal: chromeText on accent@0.16/window (#c0caf5 on #342f47) is Lc 69.7 / 7.91:1, chrome needs Lc 75 / 7:1
- terminal: mutedText on window (#8f96bc on #1a1b26) is Lc 45.0 / 5.91:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on raised (#8f96bc on #20212e) is Lc 44.1 / 5.50:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on sunken (#8f96bc on #12131a) is Lc 45.9 / 6.40:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on bubble (#8f96bc on #20212e) is Lc 44.1 / 5.50:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on control (#8f96bc on #292b3a) is Lc 42.3 / 4.83:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on hover (#8f96bc on #22232f) is Lc 43.8 / 5.38:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on accent@0.16/window (#8f96bc on #342f47) is Lc 40.9 / 4.42:1, muted needs Lc 60 / 4.5:1
- terminal: mutedText on accent@0.16/raised (#8f96bc on #39344e) is Lc 39.4 / 4.09:1, muted needs Lc 60 / 4.5:1
- terminal: faintText on window (#8085a0 on #1a1b26) is Lc 36.2 / 4.70:1, faint needs Lc 45 / 4.5:1
- terminal: faintText on raised (#8085a0 on #20212e) is Lc 35.3 / 4.38:1, faint needs Lc 45 / 4.5:1
- terminal: faintText on bubble (#8085a0 on #20212e) is Lc 35.3 / 4.38:1, faint needs Lc 45 / 4.5:1
- terminal: faintText on sunken (#8085a0 on #12131a) is Lc 37.1 / 5.10:1, faint needs Lc 45 / 4.5:1
- terminal: faintText on accent@0.16/raised (#8085a0 on #39344e) is Lc 30.6 / 3.26:1, faint needs Lc 45 / 4.5:1
- terminal: link on window (#7aa2f7 on #1a1b26) is Lc 51.1 / 6.79:1, signal needs Lc 60 / 4.5:1
- terminal: link on bubble (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- terminal: link on raised (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- terminal: accent on window (#bb9af7 on #1a1b26) is Lc 55.0 / 7.39:1, signal needs Lc 60 / 4.5:1
- terminal: accent on raised (#bb9af7 on #20212e) is Lc 54.1 / 6.89:1, signal needs Lc 60 / 4.5:1
- terminal: accent on sunken (#bb9af7 on #12131a) is Lc 55.9 / 8.01:1, signal needs Lc 60 / 4.5:1
- terminal: accentText on accent (#1a1b26 on #bb9af7) is Lc 56.4 / 7.39:1, signal needs Lc 60 / 4.5:1
- terminal: danger on window (#c98a98 on #1a1b26) is Lc 46.9 / 6.16:1, signal needs Lc 60 / 4.5:1
- terminal: danger on raised (#c98a98 on #20212e) is Lc 46.0 / 5.74:1, signal needs Lc 60 / 4.5:1
- terminal: danger on dangerSurface (#c98a98 on #2b2028) is Lc 45.8 / 5.65:1, signal needs Lc 60 / 4.5:1
- terminal: border on window (#41445a on #1a1b26) is Lc 8.7 / 1.79:1, outline needs Lc 15 / 1.5:1
- terminal: border on raised (#41445a on #20212e) is Lc 7.9 / 1.67:1, outline needs Lc 15 / 1.5:1
- terminal: success and warning are ΔE00 5.5 apart with protanopia, needs 12
- terminal: success and warning are ΔE00 0.9 apart with deuteranopia, needs 12
- terminal: warning and danger are ΔE00 9.0 apart with tritanopia, needs 12
- paper: text on accent@0.16/window (#3d2e20 on #e5d0b4) is Lc 73.6 / 8.70:1, body needs Lc 75 / 7:1
- paper: chromeText on accent@0.16/window (#3d2e20 on #e5d0b4) is Lc 73.6 / 8.70:1, chrome needs Lc 75 / 7:1
- paper: mutedText on accent@0.16/window (#6b5b4d on #e5d0b4) is Lc 56.6 / 4.34:1, muted needs Lc 60 / 4.5:1
- paper: faintText on bubble (#75654f on #f1e4c6) is Lc 62.7 / 4.46:1, faint needs Lc 45 / 4.5:1
- paper: faintText on sunken (#75654f on #ebdfc3) is Lc 59.8 / 4.26:1, faint needs Lc 45 / 4.5:1
- paper: faintText on accent@0.16/raised (#75654f on #ecd6bc) is Lc 56.1 / 4.00:1, faint needs Lc 45 / 4.5:1
- paper: link on window (#1f7a78 on #f3e8cf) is Lc 61.7 / 4.19:1, signal needs Lc 60 / 4.5:1
- paper: link on bubble (#1f7a78 on #f1e4c6) is Lc 59.5 / 4.05:1, signal needs Lc 60 / 4.5:1
- paper: accent on sunken (#9c4f24 on #ebdfc3) is Lc 60.7 / 4.46:1, signal needs Lc 60 / 4.5:1
- paper: success and warning are ΔE00 6.7 apart with protanopia, needs 12
- paper: accent and danger are ΔE00 11.4 apart with protanopia, needs 12
- paper: success and warning are ΔE00 4.6 apart with deuteranopia, needs 12
- paper: success and danger are ΔE00 2.5 apart with deuteranopia, needs 12
- paper: warning and danger are ΔE00 6.9 apart with deuteranopia, needs 12
- paper: accent and danger are ΔE00 6.7 apart with deuteranopia, needs 12
- paper: warning and danger are ΔE00 8.7 apart with tritanopia, needs 12
- paper: accent and danger are ΔE00 5.7 apart with tritanopia, needs 12
- dusk: chromeText on window (#c0caf5 on #1a1b26) is Lc 73.8 / 10.59:1, chrome needs Lc 75 / 7:1
- dusk: chromeText on raised (#c0caf5 on #20212e) is Lc 73.0 / 9.87:1, chrome needs Lc 75 / 7:1
- dusk: chromeText on control (#c0caf5 on #292b3a) is Lc 71.2 / 8.67:1, chrome needs Lc 75 / 7:1
- dusk: chromeText on hover (#c0caf5 on #22232f) is Lc 72.7 / 9.64:1, chrome needs Lc 75 / 7:1
- dusk: chromeText on accent@0.16/raised (#c0caf5 on #39344e) is Lc 68.2 / 7.33:1, chrome needs Lc 75 / 7:1
- dusk: chromeText on accent@0.16/window (#c0caf5 on #342f47) is Lc 69.7 / 7.91:1, chrome needs Lc 75 / 7:1
- dusk: mutedText on window (#a39a8c on #1a1b26) is Lc 46.7 / 6.15:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on raised (#a39a8c on #20212e) is Lc 45.8 / 5.73:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on sunken (#a39a8c on #12131a) is Lc 47.6 / 6.67:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on bubble (#a39a8c on #20212e) is Lc 45.8 / 5.73:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on control (#a39a8c on #292b3a) is Lc 44.0 / 5.04:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on hover (#a39a8c on #22232f) is Lc 45.5 / 5.60:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on accent@0.16/window (#a39a8c on #342f47) is Lc 42.6 / 4.60:1, muted needs Lc 60 / 4.5:1
- dusk: mutedText on accent@0.16/raised (#a39a8c on #39344e) is Lc 41.1 / 4.26:1, muted needs Lc 60 / 4.5:1
- dusk: faintText on window (#8a8378 on #1a1b26) is Lc 35.0 / 4.56:1, faint needs Lc 45 / 4.5:1
- dusk: faintText on raised (#8a8378 on #20212e) is Lc 34.2 / 4.25:1, faint needs Lc 45 / 4.5:1
- dusk: faintText on bubble (#8a8378 on #20212e) is Lc 34.2 / 4.25:1, faint needs Lc 45 / 4.5:1
- dusk: faintText on sunken (#8a8378 on #12131a) is Lc 35.9 / 4.94:1, faint needs Lc 45 / 4.5:1
- dusk: faintText on accent@0.16/raised (#8a8378 on #39344e) is Lc 29.4 / 3.15:1, faint needs Lc 45 / 4.5:1
- dusk: link on window (#7aa2f7 on #1a1b26) is Lc 51.1 / 6.79:1, signal needs Lc 60 / 4.5:1
- dusk: link on bubble (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- dusk: link on raised (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- dusk: accent on window (#bb9af7 on #1a1b26) is Lc 55.0 / 7.39:1, signal needs Lc 60 / 4.5:1
- dusk: accent on raised (#bb9af7 on #20212e) is Lc 54.1 / 6.89:1, signal needs Lc 60 / 4.5:1
- dusk: accent on sunken (#bb9af7 on #12131a) is Lc 55.9 / 8.01:1, signal needs Lc 60 / 4.5:1
- dusk: accentText on accent (#1a1b26 on #bb9af7) is Lc 56.4 / 7.39:1, signal needs Lc 60 / 4.5:1
- dusk: danger on window (#c98a98 on #1a1b26) is Lc 46.9 / 6.16:1, signal needs Lc 60 / 4.5:1
- dusk: danger on raised (#c98a98 on #20212e) is Lc 46.0 / 5.74:1, signal needs Lc 60 / 4.5:1
- dusk: danger on dangerSurface (#c98a98 on #2b2028) is Lc 45.8 / 5.65:1, signal needs Lc 60 / 4.5:1
- dusk: border on window (#41445a on #1a1b26) is Lc 8.7 / 1.79:1, outline needs Lc 15 / 1.5:1
- dusk: border on raised (#41445a on #20212e) is Lc 7.9 / 1.67:1, outline needs Lc 15 / 1.5:1
- dusk: success and warning are ΔE00 5.5 apart with protanopia, needs 12
- dusk: success and warning are ΔE00 0.9 apart with deuteranopia, needs 12
- dusk: warning and danger are ΔE00 9.0 apart with tritanopia, needs 12
- hyperlegible: chromeText on window (#c0caf5 on #1a1b26) is Lc 73.8 / 10.59:1, chrome needs Lc 75 / 7:1
- hyperlegible: chromeText on raised (#c0caf5 on #20212e) is Lc 73.0 / 9.87:1, chrome needs Lc 75 / 7:1
- hyperlegible: chromeText on control (#c0caf5 on #292b3a) is Lc 71.2 / 8.67:1, chrome needs Lc 75 / 7:1
- hyperlegible: chromeText on hover (#c0caf5 on #22232f) is Lc 72.7 / 9.64:1, chrome needs Lc 75 / 7:1
- hyperlegible: chromeText on accent@0.16/raised (#c0caf5 on #39344e) is Lc 68.2 / 7.33:1, chrome needs Lc 75 / 7:1
- hyperlegible: chromeText on accent@0.16/window (#c0caf5 on #342f47) is Lc 69.7 / 7.91:1, chrome needs Lc 75 / 7:1
- hyperlegible: mutedText on window (#a6adc8 on #1a1b26) is Lc 56.7 / 7.68:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on raised (#a6adc8 on #20212e) is Lc 55.9 / 7.15:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on sunken (#a6adc8 on #12131a) is Lc 57.6 / 8.32:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on bubble (#a6adc8 on #20212e) is Lc 55.9 / 7.15:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on control (#a6adc8 on #292b3a) is Lc 54.1 / 6.28:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on hover (#a6adc8 on #22232f) is Lc 55.6 / 6.99:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on accent@0.16/window (#a6adc8 on #342f47) is Lc 52.6 / 5.74:1, muted needs Lc 60 / 4.5:1
- hyperlegible: mutedText on accent@0.16/raised (#a6adc8 on #39344e) is Lc 51.1 / 5.31:1, muted needs Lc 60 / 4.5:1
- hyperlegible: faintText on window (#8d93b0 on #1a1b26) is Lc 43.1 / 5.64:1, faint needs Lc 45 / 4.5:1
- hyperlegible: faintText on raised (#8d93b0 on #20212e) is Lc 42.3 / 5.26:1, faint needs Lc 45 / 4.5:1
- hyperlegible: faintText on bubble (#8d93b0 on #20212e) is Lc 42.3 / 5.26:1, faint needs Lc 45 / 4.5:1
- hyperlegible: faintText on sunken (#8d93b0 on #12131a) is Lc 44.0 / 6.11:1, faint needs Lc 45 / 4.5:1
- hyperlegible: faintText on accent@0.16/raised (#8d93b0 on #39344e) is Lc 37.5 / 3.91:1, faint needs Lc 45 / 4.5:1
- hyperlegible: link on window (#7aa2f7 on #1a1b26) is Lc 51.1 / 6.79:1, signal needs Lc 60 / 4.5:1
- hyperlegible: link on bubble (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- hyperlegible: link on raised (#7aa2f7 on #20212e) is Lc 50.3 / 6.32:1, signal needs Lc 60 / 4.5:1
- hyperlegible: accent on window (#bb9af7 on #1a1b26) is Lc 55.0 / 7.39:1, signal needs Lc 60 / 4.5:1
- hyperlegible: accent on raised (#bb9af7 on #20212e) is Lc 54.1 / 6.89:1, signal needs Lc 60 / 4.5:1
- hyperlegible: accent on sunken (#bb9af7 on #12131a) is Lc 55.9 / 8.01:1, signal needs Lc 60 / 4.5:1
- hyperlegible: accentText on accent (#1a1b26 on #bb9af7) is Lc 56.4 / 7.39:1, signal needs Lc 60 / 4.5:1
- hyperlegible: danger on window (#c98a98 on #1a1b26) is Lc 46.9 / 6.16:1, signal needs Lc 60 / 4.5:1
- hyperlegible: danger on raised (#c98a98 on #20212e) is Lc 46.0 / 5.74:1, signal needs Lc 60 / 4.5:1
- hyperlegible: danger on dangerSurface (#c98a98 on #2b2028) is Lc 45.8 / 5.65:1, signal needs Lc 60 / 4.5:1
- hyperlegible: border on window (#41445a on #1a1b26) is Lc 8.7 / 1.79:1, outline needs Lc 15 / 1.5:1
- hyperlegible: border on raised (#41445a on #20212e) is Lc 7.9 / 1.67:1, outline needs Lc 15 / 1.5:1
- hyperlegible: success and warning are ΔE00 5.5 apart with protanopia, needs 12
- hyperlegible: success and warning are ΔE00 0.9 apart with deuteranopia, needs 12
- hyperlegible: warning and danger are ΔE00 9.0 apart with tritanopia, needs 12
