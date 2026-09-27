import QtQuick

// Builds a closed panel only when it is first shown, then keeps it. Creating
// every closed panel up front cost ~130 ms before the first frame and ~25 MB
// and ~1,200 items per instance for panels most sessions never open. The
// panel's own `visible` reports the loader's, so its visibility handlers
// behave as when it was created eagerly. Show the panel through the loader
// (`visible = true` on this item). A panel that hides itself (`visible =
// false` inside it) hides the loader too, and showing the loader again shows
// the panel, so dialogs written to toggle their own visibility work unchanged.
//
// The panel is named by `url`, not declared inline: an inline declaration
// makes the engine load the panel's file and every type it uses together
// with Main.qml, ~23 ms of type loading before the first frame. `properties`
// seeds it when it is built (required properties included), `handlers` maps
// its signal names to functions, and Binding elements keep changing values
// in step. The panel's type is deliberately unknown here, so the few calls
// into it go through call() and value().
Loader {
    id: loader

    required property url url
    property var properties: ({})
    property var handlers: ({})
    property bool used: false

    function build() {
        if (item !== null || !active)
            return;
        setSource(url, properties);
        const panel = item as QtObject;
        if (panel === null)
            return;
        for (const name in handlers)
            panel[name].connect(handlers[name]);
    }
    function call(method, ...args) {
        const panel = item as QtObject;
        return panel === null ? undefined : panel[method](...args);
    }
    function value(name, fallback) {
        const panel = item as QtObject;
        return panel === null ? fallback : panel[name];
    }

    visible: false
    active: used || visible
    onActiveChanged: build()
    Component.onCompleted: build()
    onVisibleChanged: {
        if (!visible)
            return;
        used = true;
        const panel = item as Item;
        if (panel !== null)
            panel.visible = true;
    }
    // A panel declared `visible: false` is shown after it is built, so its
    // own onVisibleChanged setup runs on the first show as on later ones.
    onLoaded: {
        const panel = item as Item;
        if (visible && panel !== null && !panel.visible)
            panel.visible = true;
    }

    Connections {
        target: loader.item
        function onVisibleChanged() {
            const panel = loader.item as Item;
            if (panel !== null && !panel.visible && loader.visible)
                loader.visible = false;
        }
    }
}
