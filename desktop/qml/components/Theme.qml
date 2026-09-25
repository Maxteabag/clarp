pragma Singleton
import QtQuick

// Application-wide colour and type roles. Main.qml binds `style` to the
// controller's reading theme; components and tests that never do so keep
// the terminal palette through the fallbacks below.
QtObject {
    id: theme
    property var style: ({})
    function pick(key, fallback) {
        const value = theme.style ? theme.style[key] : undefined;
        return value === undefined || value === null || value === "" ? fallback : value;
    }
    readonly property bool light: Boolean(pick("light", false))
    readonly property string fontFamily: String(pick("fontFamily", "JetBrains Mono"))
    readonly property int fontPixelSize: Number(pick("fontPixelSize", 15))
    readonly property int measure: Number(pick("measure", 840))
    // Surfaces, from deepest to most raised.
    readonly property color sunken: pick("sunken", "#12131a")
    readonly property color window: pick("window", "#1a1b26")
    readonly property color surface: pick("background", "#1a1b26")
    readonly property color raised: pick("raised", "#20212e")
    readonly property color control: pick("control", "#292b3a")
    readonly property color hover: pick("hover", "#22232f")
    readonly property color border: pick("border", "#41445a")
    readonly property color rule: pick("rule", "#303342")
    readonly property color shadow: pick("shadow", "#14151d")
    readonly property color scrim: pick("scrim", "#aa08090f")
    // Text.
    readonly property color text: pick("chromeText", "#c0caf5")
    readonly property color body: pick("text", "#e7e1dc")
    readonly property color secondary: pick("secondary", "#9ca1bd")
    readonly property color muted: pick("mutedText", "#8d93b0")
    readonly property color faint: pick("faintText", "#62677e")
    // Semantic.
    readonly property color accent: pick("accent", "#bb9af7")
    readonly property color accentText: pick("accentText", "#1a1b26")
    readonly property color link: pick("link", "#82aaff")
    readonly property color warning: pick("warning", "#e0af68")
    readonly property color danger: pick("danger", "#c98a98")
    readonly property color dangerSurface: pick("dangerSurface", "#2b2028")
    readonly property color success: pick("success", "#9ece6a")
    readonly property color selection: pick("selection", "#6f527b")
    readonly property color selectedText: pick("selectedText", "#fff8ff")
}
