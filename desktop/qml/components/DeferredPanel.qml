import QtQuick

// Builds a closed panel only when it is first shown, then keeps it. Creating
// every closed panel up front cost ~130 ms before the first frame and ~25 MB
// and ~1,200 items per instance for panels most sessions never open. The
// panel's own `visible` reports the loader's, so its visibility handlers
// behave as when it was created eagerly. Show the panel through the loader
// (`visible = true` on this item). A panel that hides itself (`visible =
// false` inside it) hides the loader too, and showing the loader again shows
// the panel, so dialogs written to toggle their own visibility work unchanged.
Loader {
    id: loader

    property bool used: false

    visible: false
    active: used || visible
    onVisibleChanged: {
        if (!visible)
            return;
        used = true;
        if (item !== null)
            item.visible = true;
    }
    // A panel declared `visible: false` is shown after it is built, so its
    // own onVisibleChanged setup runs on the first show as on later ones.
    onLoaded: if (visible && !item.visible) item.visible = true

    Connections {
        target: loader.item
        function onVisibleChanged() {
            if (loader.item !== null && !loader.item.visible && loader.visible)
                loader.visible = false;
        }
    }
}
