import QtQuick
import QtTest
import "../../qml/components" as Clarp
TestCase {
    name: "ThemeSingleton"
    function init() { Clarp.Theme.style = {}; }
    function test_fallbacksAreTheTerminalPalette() {
        compare(String(Clarp.Theme.window), "#1a1b26");
        compare(String(Clarp.Theme.text), "#c0caf5");
        compare(String(Clarp.Theme.accent), "#bb9af7");
        compare(Clarp.Theme.fontFamily, "JetBrains Mono");
        compare(Clarp.Theme.light, false);
    }
    function test_styleOverridesEveryRoleAndEmptyStringsFallBack() {
        Clarp.Theme.style = {window: "#fbf0d9", chromeText: "#3d2e20", accent: "", light: true, fontFamily: "Literata", fontPixelSize: 17};
        compare(String(Clarp.Theme.window), "#fbf0d9");
        compare(String(Clarp.Theme.text), "#3d2e20");
        compare(String(Clarp.Theme.accent), "#bb9af7");
        compare(Clarp.Theme.light, true);
        compare(Clarp.Theme.fontFamily, "Literata");
        compare(Clarp.Theme.fontPixelSize, 17);
    }
}
