import QtQuick

// Builds a closed panel only when it is first shown, then keeps it. Creating
// every closed panel up front cost ~130 ms before the first frame and ~25 MB
// and ~1,200 items per instance for panels most sessions never open. The
// panel's own `visible` reports the loader's, so its visibility handlers
// behave as when it was created eagerly. Hide the panel through the loader
// (`visible = false` on this item).
Loader {
    property bool used: false

    visible: false
    active: used || visible
    onVisibleChanged: if (visible) used = true
}
