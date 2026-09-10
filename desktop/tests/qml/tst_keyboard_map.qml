import QtQuick
import QtTest
import "../../qml/components"

TestCase {
    name: "KeyboardHierarchy"
    KeyboardMap { id: map }
    function actions() { return map.activeBindings.map(entry => entry.action); }
    function keys() { return map.shortcuts.map(entry => entry.key); }
    function init() { map.contextName = "pane"; map.hasAgent = true; map.hasRows = true; map.canSend = true; map.hasAttention = true; }
    function test_inheritanceAndOverrides() {
        verify(keys().includes("E"));
        verify(keys().includes("Ctrl+B"));
        verify(keys().includes("Alt+V"));
        compare(actions().filter(action => action === "split-right").length, 1);
        compare(new Set(keys()).size, keys().length);
        map.contextName = "sidebar";
        verify(keys().includes("J"));
        verify(keys().includes("Return"));
        verify(!keys().includes("Alt+V"));
        verify(keys().includes("Ctrl+Alt+V"));
    }
    function test_typingAndModalIsolation() {
        for (const state of ["composer", "search"]) {
            map.contextName = state;
            for (const key of ["E", "C", "I", "J", "K", "Space", "Tab", "Return"])
                verify(!keys().includes(key), state + " stole " + key);
            verify(keys().includes("Escape"));
            verify(keys().includes("Ctrl+Shift+K"));
        }
        map.contextName = "modal";
        compare(keys(), ["Escape"]);
        verify(!keys().includes("Ctrl+J"));
        map.contextName = "blocked";
        compare(keys(), []);
    }
    function test_guardsAndFooterUseSameResolution() {
        verify(keys().includes("N"));
        map.hasAttention = false;
        verify(!keys().includes("N"));
        verify(!map.hints.some(entry => entry.action === "next-attention"));
        map.hasAgent = false;
        verify(!actions().includes("release-agent"));
        verify(!actions().includes("focus-composer"));
        verify(!map.hints.some(entry => entry.action === "focus-composer"));
        map.contextName = "sidebar";
        map.hasRows = false;
        verify(!keys().includes("Return"));
        verify(!map.hints.some(entry => entry.action === "agent-open"));
        for (const state of Object.keys(map.states)) {
            map.contextName = state;
            compare(new Set(keys()).size, keys().length, state + " duplicate shortcut");
            for (const hint of map.hints)
                verify(map.activeBindings.includes(hint));
        }
    }
}
