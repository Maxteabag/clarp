# Native component verification

The component runner compiles the actual PaneTreeModel, never a copied model.
It uses a temporary settings directory and synthetic session names. The normal
BUILD_TESTING build registers it as clarp-desktop-pane-contract. QML component
checks (including keymap import rejection and pooled expansion reset) run in the
existing clarp-desktop-activity-layout lane. Full-application keyboard/scroll
lanes remain necessary: the narrow runner cannot prove controller integration.

    cmake --preset release -S desktop
    cmake --build desktop/build/release --parallel 2
    ctest --test-dir desktop/build/release --output-on-failure
    cmake --build desktop/build/release --target all_qmllint

A smaller model-only build remains available:

    cmake -S desktop/research/component -B /absolute/private-build
    cmake --build /absolute/private-build
    /absolute/private-build/pane-contract

`capture.sh keys|composer|workspaces|zoom|lifecycle /absolute/private-output`
uses the maintained qt-clarp-desktop offscreen helper and this checkout's release
binary. The helper must be installed locally; it isolates settings/runtime and
never drives the visible desktop. The fixture rejects non-offscreen rendering
and uses synthetic conversations with a deliberately unavailable Host. Capture
source commit and image hashes with each receipt. Lifecycle JSON measures object
survival and reader state, not GPU speed or physical frame rate. Capture controls
are exercised only when CLARP_DESKTOP_RESEARCH is explicitly set.

The implemented workspace slice retains views and supports create/switch/menu
move, with eight workspace and existing pane bounds. It does not implement
rename/archive or drag/drop. Concurrent saves use a dedicated short-lived file
lock and compare the last observed collection. A stale window preserves its
layout under workspace/recovery in QSettings and shows an explicit replacement
action. Recovery snapshots survive restart; a recovery-history chooser is not
included. Tests exercise two independent model instances, not physical windows.
