#pragma once
#include <QList>
#include <QString>
#include <QStringList>
#include <QVariantList>
#include <QVariantMap>
#include <functional>

namespace clarp {

// A reading theme restyles only the parts of a chat that are read for long
// stretches: the transcript surface, message text and the composer editor.
// The surrounding chrome keeps the terminal palette so the panes still read
// as one application.
//
// Choices follow the reading research summarised in docs/reading-themes.md:
// body text at or above 7:1 (WCAG AAA), secondary text at or above 4.5:1,
// off-white rather than pure white on dark surfaces to avoid halation, warm
// paper rather than pure white on light surfaces, and a measure that keeps
// lines in the 60–90 character range at the theme's body size.
struct ReadingTheme {
    QString id{};
    QString label{};
    QString detail{};
    QStringList fontFamilies;  // First installed family wins.
    int fontPixelSize = 15;
    int measure = 840;         // Maximum message width in pixels.
    int radius = 0;            // Corner radius for controls, cards and bubbles.
    QString background{};
    QString bubble{};            // The current user's own messages.
    QString text{};
    QString mutedText{};         // Reply markers, tool activity, section headings.
    QString faintText{};         // Timestamps and group toggles.
    QString rule{};              // Section separators.
    QString selection{};
    QString selectedText{};
    QString link{};
    // Application chrome. The transcript roles above stay the reading
    // surface; these restyle every other pane, dialog and control so the
    // window reads as one theme.
    bool light = false;
    QString sunken{};        // Deepest surface: tab strip, status bar.
    QString window{};        // Sidebar, panels, dialogs.
    QString raised{};        // Cards, popups, alternate rows.
    QString control{};       // Buttons, fields.
    QString hover{};
    QString border{};
    QString shadow{};
    QString scrim{};         // Modal backdrop (with alpha).
    QString chromeText{};    // Labels and controls.
    QString secondary{};     // Less prominent labels.
    QString accent{};
    QString accentText{};
    QString warning{};
    QString danger{};
    QString dangerSurface{};
    QString success{};
};

// The chrome roles every dark theme shares: the terminal palette.
struct ChromeRoles {
    bool light = false;
    QString sunken, window, raised, control, hover, border, shadow, scrim, chromeText,
        secondary, accent, accentText, warning, danger, dangerSurface, success;
};

inline ChromeRoles darkChrome() {
    return {.light = false,
            .sunken = QStringLiteral("#12131a"),
            .window = QStringLiteral("#1a1b26"),
            .raised = QStringLiteral("#20212e"),
            .control = QStringLiteral("#292b3a"),
            .hover = QStringLiteral("#22232f"),
            .border = QStringLiteral("#41445a"),
            .shadow = QStringLiteral("#14151d"),
            .scrim = QStringLiteral("#aa08090f"),
            .chromeText = QStringLiteral("#c0caf5"),
            .secondary = QStringLiteral("#9ca1bd"),
            .accent = QStringLiteral("#bb9af7"),
            .accentText = QStringLiteral("#1a1b26"),
            .warning = QStringLiteral("#e0af68"),
            .danger = QStringLiteral("#c98a98"),
            .dangerSurface = QStringLiteral("#2b2028"),
            .success = QStringLiteral("#9ece6a")};
}

// Warm paper chrome for the light theme. Every text role keeps at least
// 4.5:1 against its surface; accent and danger are darkened from the
// CloudEpub values so they also pass as text on paper.
inline ChromeRoles paperChrome() {
    return {.light = true,
            .sunken = QStringLiteral("#ebdfc3"),
            .window = QStringLiteral("#f3e8cf"),
            .raised = QStringLiteral("#fbf0d9"),
            .control = QStringLiteral("#e6d8b8"),
            .hover = QStringLiteral("#efe3c7"),
            .border = QStringLiteral("#cdbd9b"),
            .shadow = QStringLiteral("#d9cba9"),
            .scrim = QStringLiteral("#8c3a2a16"),
            .chromeText = QStringLiteral("#3d2e20"),
            .secondary = QStringLiteral("#5c4b3b"),
            .accent = QStringLiteral("#9c4f24"),
            .accentText = QStringLiteral("#fbf0d9"),
            .warning = QStringLiteral("#7a4b00"),
            .danger = QStringLiteral("#9a3434"),
            .dangerSurface = QStringLiteral("#f2d8cc"),
            .success = QStringLiteral("#3f6b1f")};
}

inline ReadingTheme withChrome(ReadingTheme theme, const ChromeRoles& chrome) {
    theme.light = chrome.light;
    theme.sunken = chrome.sunken;
    theme.window = chrome.window;
    theme.raised = chrome.raised;
    theme.control = chrome.control;
    theme.hover = chrome.hover;
    theme.border = chrome.border;
    theme.shadow = chrome.shadow;
    theme.scrim = chrome.scrim;
    theme.chromeText = chrome.chromeText;
    theme.secondary = chrome.secondary;
    theme.accent = chrome.accent;
    theme.accentText = chrome.accentText;
    theme.warning = chrome.warning;
    theme.danger = chrome.danger;
    theme.dangerSurface = chrome.dangerSurface;
    theme.success = chrome.success;
    return theme;
}

inline const QList<ReadingTheme>& readingThemes() {
    static const QList<ReadingTheme> themes = {
        withChrome(ReadingTheme{
            .id = QStringLiteral("terminal"),
            .label = QStringLiteral("Terminal"),
            .detail = QStringLiteral("JetBrains Mono on the dark palette. The default."),
            .fontFamilies = {QStringLiteral("JetBrains Mono")},
            .fontPixelSize = 15,
            .measure = 840,
            .background = QStringLiteral("#1a1b26"),
            .bubble = QStringLiteral("#20212e"),
            .text = QStringLiteral("#e7e1dc"),
            .mutedText = QStringLiteral("#8f96bc"),
            .faintText = QStringLiteral("#8085a0"),
            .rule = QStringLiteral("#303342"),
            .selection = QStringLiteral("#6f527b"),
            .selectedText = QStringLiteral("#fff8ff"),
            .link = QStringLiteral("#7aa2f7"),
        }, darkChrome()),
        withChrome(ReadingTheme{
            .id = QStringLiteral("paper"),
            .label = QStringLiteral("Paper"),
            .detail = QStringLiteral("Literata on warm sepia paper, as in the CloudEpub reader. Light and low-glare."),
            .fontFamilies = {QStringLiteral("Literata"), QStringLiteral("Noto Serif"), QStringLiteral("Liberation Serif"), QStringLiteral("serif")},
            .fontPixelSize = 17,
            .measure = 720,
            .radius = 8,
            .background = QStringLiteral("#fbf0d9"),
            .bubble = QStringLiteral("#f1e4c6"),
            .text = QStringLiteral("#3d2e20"),
            .mutedText = QStringLiteral("#6b5b4d"),
            .faintText = QStringLiteral("#75654f"),
            .rule = QStringLiteral("#dccdb0"),
            .selection = QStringLiteral("#e6c48f"),
            .selectedText = QStringLiteral("#1f1a16"),
            .link = QStringLiteral("#1f7a78"),
        }, paperChrome()),
        withChrome(ReadingTheme{
            .id = QStringLiteral("dusk"),
            .label = QStringLiteral("Dusk"),
            .detail = QStringLiteral("Literata in warm off-white on the dark palette. A book at night."),
            .fontFamilies = {QStringLiteral("Literata"), QStringLiteral("Noto Serif"), QStringLiteral("Liberation Serif"), QStringLiteral("serif")},
            .fontPixelSize = 17,
            .measure = 720,
            .radius = 8,
            .background = QStringLiteral("#1a1b26"),
            .bubble = QStringLiteral("#20212e"),
            .text = QStringLiteral("#e2d9cb"),
            .mutedText = QStringLiteral("#a39a8c"),
            .faintText = QStringLiteral("#8a8378"),
            .rule = QStringLiteral("#303342"),
            .selection = QStringLiteral("#6f527b"),
            .selectedText = QStringLiteral("#fff8ff"),
            .link = QStringLiteral("#7aa2f7"),
        }, darkChrome()),
        withChrome(ReadingTheme{
            .id = QStringLiteral("hyperlegible"),
            .label = QStringLiteral("Hyperlegible"),
            .detail = QStringLiteral("Atkinson Hyperlegible sans on the dark palette. Built for low vision and tired eyes."),
            .fontFamilies = {QStringLiteral("Atkinson Hyperlegible Next"), QStringLiteral("Atkinson Hyperlegible"), QStringLiteral("Noto Sans"), QStringLiteral("sans-serif")},
            .fontPixelSize = 16,
            .measure = 760,
            .radius = 8,
            .background = QStringLiteral("#1a1b26"),
            .bubble = QStringLiteral("#20212e"),
            .text = QStringLiteral("#e5e7eb"),
            .mutedText = QStringLiteral("#a6adc8"),
            .faintText = QStringLiteral("#8d93b0"),
            .rule = QStringLiteral("#303342"),
            .selection = QStringLiteral("#6f527b"),
            .selectedText = QStringLiteral("#fff8ff"),
            .link = QStringLiteral("#7aa2f7"),
        }, darkChrome()),
    };
    return themes;
}

inline QString defaultReadingThemeId() { return QStringLiteral("terminal"); }

inline const ReadingTheme& readingTheme(const QString& id) {
    for (const auto& theme : readingThemes())
        if (theme.id == id) return theme;
    return readingThemes().first();
}

inline QString normalizedReadingThemeId(const QString& id) { return readingTheme(id).id; }

// `installed` reports whether a family is available; generic CSS names such as
// "serif" are always accepted so Qt's own matching can take over.
inline QString resolveReadingFont(const ReadingTheme& theme,
                                  const std::function<bool(const QString&)>& installed) {
    for (const auto& family : theme.fontFamilies) {
        if (family == QStringLiteral("serif") || family == QStringLiteral("sans-serif")
            || family == QStringLiteral("monospace") || installed(family))
            return family;
    }
    return theme.fontFamilies.isEmpty() ? QStringLiteral("JetBrains Mono") : theme.fontFamilies.last();
}

inline QVariantMap readingThemeStyle(const QString& id,
                                     const std::function<bool(const QString&)>& installed) {
    const auto& theme = readingTheme(id);
    return {
        {QStringLiteral("id"), theme.id},
        {QStringLiteral("label"), theme.label},
        {QStringLiteral("fontFamily"), resolveReadingFont(theme, installed)},
        {QStringLiteral("fontPixelSize"), theme.fontPixelSize},
        {QStringLiteral("measure"), theme.measure},
        {QStringLiteral("radius"), theme.radius},
        {QStringLiteral("background"), theme.background},
        {QStringLiteral("bubble"), theme.bubble},
        {QStringLiteral("text"), theme.text},
        {QStringLiteral("mutedText"), theme.mutedText},
        {QStringLiteral("faintText"), theme.faintText},
        {QStringLiteral("rule"), theme.rule},
        {QStringLiteral("selection"), theme.selection},
        {QStringLiteral("selectedText"), theme.selectedText},
        {QStringLiteral("link"), theme.link},
        {QStringLiteral("light"), theme.light},
        {QStringLiteral("sunken"), theme.sunken},
        {QStringLiteral("window"), theme.window},
        {QStringLiteral("raised"), theme.raised},
        {QStringLiteral("control"), theme.control},
        {QStringLiteral("hover"), theme.hover},
        {QStringLiteral("border"), theme.border},
        {QStringLiteral("shadow"), theme.shadow},
        {QStringLiteral("scrim"), theme.scrim},
        {QStringLiteral("chromeText"), theme.chromeText},
        {QStringLiteral("secondary"), theme.secondary},
        {QStringLiteral("accent"), theme.accent},
        {QStringLiteral("accentText"), theme.accentText},
        {QStringLiteral("warning"), theme.warning},
        {QStringLiteral("danger"), theme.danger},
        {QStringLiteral("dangerSurface"), theme.dangerSurface},
        {QStringLiteral("success"), theme.success},
    };
}

inline QVariantList readingThemeOptions() {
    QVariantList options;
    for (const auto& theme : readingThemes()) {
        options.append(QVariantMap{
            {QStringLiteral("id"), theme.id},
            {QStringLiteral("label"), theme.label},
            {QStringLiteral("detail"), theme.detail},
            {QStringLiteral("fontFamily"), theme.fontFamilies.first()},
        });
    }
    return options;
}

}  // namespace clarp
