# Reading themes

Appearance → Reading theme restyles the whole window. The transcript roles
(surface, message text, the user's own bubble, the composer editor) carry the
reading research below; the chrome roles (sidebar, panels, dialogs, controls,
status bar) follow so split panes and dialogs read as one theme. Dark themes
share the terminal chrome; Paper swaps in a warm light chrome. The choice is
saved on the device under `appearance/readingTheme` and is also available
from Ctrl+K as `Reading theme: …` rows; the current one is marked. In
Settings, Enter cycles the themes and Left/Right step through them.

## How colours reach the UI

`qml/components/Theme.qml` is a QML singleton whose roles (`Theme.window`,
`Theme.text`, `Theme.accent`, …) fall back to the terminal palette and are
overridden by `controller.readingStyle`, which `Main.qml` binds once. QML
components use those roles instead of hex literals; `qml/components/qmldir`
registers the singleton for the source-directory imports the QML tests use.
The C++ side mirrors the same mapping into the application `QPalette`
(`DesktopPalette.h`) on every theme change, so Qt Quick Controls Basic picks
up checked fills, popups and disabled text too. Add a new colour by adding a
role to `ReadingTheme.h`, `readingThemeStyle()`, and `Theme.qml`; never a new
literal in a component.

`Theme.radius` is the one non-colour role: 0 on Terminal, so the TUI keeps
its square identity, and 8 on the reading themes. Controls, cards and bubbles
use it. Badges, avatars, the rail buttons, the sidebar plus button and the
scope chips are round in every theme; a count pill or a portrait has no
reason to be square.

| Theme | Font | Body | Text on surface | Secondary text | Intended for |
| --- | --- | --- | --- | --- | --- |
| Terminal | JetBrains Mono | 15 px | 13.2:1 | ≥ 4.7:1 | The default; unchanged look, timestamps lifted from 2.5:1 |
| Paper | Literata | 17 px | 11.5:1 | ≥ 5.0:1 | Long reading in daylight; the CloudEpub sepia reader |
| Dusk | Literata | 17 px | 12.2:1 | ≥ 4.6:1 | Long reading at night on the dark palette |
| Hyperlegible | Atkinson Hyperlegible | 16 px | 13.8:1 | ≥ 5.6:1 | Low vision, tired eyes, small text |

`tests/tst_reading_theme.cpp` asserts these floors (7:1 for body text, 4.5:1
for secondary text and links, no pure white-on-black or black-on-white), plus
the size and measure ranges below, so a colour tweak that breaks readability
fails the build.

## What the research says, and how the themes apply it

- **Typeface.** Controlled studies find no reliable legibility gap between
  serif and sans-serif on modern screens; what matters is a large x-height,
  open counters, unambiguous letterforms and true text sizing. Literata was
  commissioned by Google for Play Books and, like Amazon's Bookerly, is drawn
  for screen reading with optical sizes. Atkinson Hyperlegible (Braille
  Institute) exaggerates letter differences for low vision. A monospace font
  is right for code and for the terminal identity, but its uniform advance
  and low character density cost reading speed on prose, so the reading
  themes switch to a proportional face for chat text and leave code blocks to
  Qt's fixed font.
- **Contrast.** WCAG AA is 4.5:1; AAA is 7:1 for body text. On dark surfaces
  the eye responds more strongly to light-on-dark, so pure white on black
  causes halation (glow that blurs strokes) for many readers, and reading
  speed drops with extreme polarity. Off-white in the 12–15:1 range is the
  recommended band and every dark theme here lands in it. On light surfaces
  pure white glares; warm paper such as CloudEpub's `#FBF0D9` lowers luminance
  without lowering legibility, and a dark warm brown rather than pure black
  keeps the pairing under the harshness threshold while staying above 11:1.
- **Size and measure.** 16–18 px body text at arm's length and 50–90
  characters per line are the consistently reported comfort range; longer
  lines make the return sweep error-prone, shorter ones break rhythm. The
  proportional themes cap a message at 720–760 px, which is roughly 85–95
  characters at their size. The terminal theme keeps its 840 px cap because
  agent output is often code and tables that benefit from width.
- **Line height.** 1.4–1.6 is the usual advice. Qt's `TextEdit` does not
  expose line height for Markdown, so the themes rely on Literata's and
  Atkinson's generous built-in leading and slightly larger sizes instead.

## Fallbacks

Fonts are resolved in C++ from an ordered list (for example Literata → Noto
Serif → Liberation Serif → `serif`), so a machine without the first family
degrades to the next installed one rather than to the platform default. An
unknown saved theme id falls back to Terminal. Test stubs that do not expose
`readingStyle` render with the terminal colours.

## Screenshot hook

`CLARP_SCREENSHOT_READING_THEME=<id>` applies a theme in screenshot mode only;
combine it with `CLARP_SCREENSHOT_SCENARIO=markdown` for a populated
transcript. Normal launches never take the theme from the environment.

## Sources

- EditionGuard, "Best Fonts for eBooks" (Literata, Bookerly, Georgia):
  https://www.editionguard.com/learn/best-fonts-e-books/
- Wikipedia, "Atkinson Hyperlegible" (design goals, 2025 Next release):
  https://en.wikipedia.org/wiki/Atkinson_Hyperlegible
- ColorContrast, "Dark Mode Contrast: WCAG-Compliant Dark UI" (halation,
  7:1 target, off-white recommendation):
  https://www.colorcontrast.org/blog/dark-mode-contrast-accessibility-guide/
- ColorContrast, "Dark Mode Color Combinations That Pass WCAG":
  https://www.colorcontrast.org/blog/dark-mode-color-combinations-tested/
- UXPin, "Optimal Line Length for Readability: The 50–75 Character Rule":
  https://www.uxpin.com/studio/blog/optimal-line-length-for-readability/
- Baymard Institute, "Readability: The Optimal Line Length":
  https://baymard.com/blog/line-length-readability
- a11y-blog, "Is sepia mode the default feature?":
  https://a11y-blog.dev/en/articles/is-sepia-mode-essential/
