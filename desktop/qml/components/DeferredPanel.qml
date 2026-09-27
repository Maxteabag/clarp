import QtQuick

// Builds a hidden panel off the startup path. Creating every closed panel
// while Main.qml loaded cost ~130 ms before the first frame; now each one is
// incubated in the background once `ready` is set after that frame, or built
// at once if something shows it first. The panel's own `visible` reports the
// loader's, so its visibility handlers behave as when it was created eagerly.
// Hide the panel through the loader (`visible = false` on this item).
Loader {
    property bool ready: false

    visible: false
    active: ready || visible
    asynchronous: !visible
}
