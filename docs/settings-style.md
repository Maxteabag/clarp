# Settings style contract

Settings is the largest chrome surface in the app, and it used to carry two
different design languages one tap apart: a bespoke themed landing page, and
thirteen stock `Form`s behind it. This is the single style everything in
`SettingsView.swift` now follows. `ios-native/scripts/verify_static.py` enforces
the mechanical parts of it.

## Surface

Native `Form` (or `List`) for structure and controls, dressed in the app theme.
Every container ends with `.clarpSettingsSurface()`, which supplies the themed
background and the row fill that matches the landing page cards:

```swift
Form {
    …
}
.clarpSettingsSurface()
.navigationTitle("…")
```

Native controls are kept deliberately. Hand-rolling the 42 toggles, pickers and
text fields would forfeit Dynamic Type, VoiceOver, keyboard handling and the
iOS 26 Liquid Glass behaviours for a look the theme already provides.

## Colour

Read colour through the semantic roles in `Kuro`, never through SwiftUI's system
colours and never through the raw palette. The name says what the colour
*means*, so a themed surface and a native control agree on it.

| Role | Use for |
| --- | --- |
| `Kuro.textPrimary` | body text |
| `Kuro.textSecondary` | explanations, values, captions |
| `Kuro.textTertiary` | chevrons, separators, disabled hints |
| `Kuro.affirmative` | on, verified, succeeded — including every `Toggle` tint |
| `Kuro.caution` | a recoverable problem the user can still act on |
| `Kuro.danger` | destructive, failed, blocked |
| `Kuro.settingsRowFill` | the fill behind a grouped row |
| `Kuro.settingsNestedFill` | a card nested inside a row |

`.foregroundStyle(.secondary/.primary/.tertiary/.orange/.red/.green)` and
`.tint(.green)` are rejected by static verification.

Glyphs drawn on a coloured fill need a colour that inverts with it:
`Kuro.glyphOnAffirmative` on `affirmative`, `Kuro.glyphOnCategory` on the
`category*` tints. A fixed black or white loses contrast in one appearance.

## Reporting outcomes

One component, `ClarpStatusBanner`, and severity is decided where the outcome is
known — never re-derived from the message text:

```swift
@State private var skillsError: ClarpStatus?
…
skillsError = .failure("Couldn’t load skills", error)
…
if let skillsError { ClarpStatusBanner(status: skillsError) }
```

`ClarpStatus` carries `.info`, `.success`, `.caution` and `.failure`. Sniffing a
prefix out of the copy (`text.hasPrefix("Couldn’t")`) breaks the moment a message
is reworded or localised, so static verification rejects it.

A modal `.alert` is still right for something the user must acknowledge before
continuing, such as a started software update.

## Explanatory copy

`SettingsExplainer("…")` — one component, so the caption style and wrapping
behaviour cannot drift between pages. Use a `Section`'s native `footer:` when the
text describes the whole section rather than the control above it.

## Rows and buttons

- `LabeledContent` for label/value, `Toggle` for booleans, `NavigationLink` with
  a `Label` for drill-in. Prefer these to a hand-built `HStack … Spacer()`.
- `.borderedProminent` for the one primary action on a screen, `.bordered` for a
  secondary action, `.borderless` for an action living inside a row.
- Destructive actions use `role: .destructive`; never paint a button red by hand.

## Computer settings shape

The Computer landing page lists what *runs on* the Computer. Editing the
Computer itself — its connection and its paired devices — lives behind the
toolbar's edit pencil (`ComputerSettingsLandingPolicy.editDestination`), not in a
row alongside the configuration destinations.

Each destination loads only the resources it renders; see `loadCategory()`.
