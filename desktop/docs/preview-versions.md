# Preview update and rollback

The permanent preview updater prepares and verifies main without closing the
running window. PreviewVersions reads its local catalog asynchronously every
15 seconds. A hash of `/proc/self/exe` identifies the actual running image even
after the installed pathname is atomically replaced. A versioned Update button
appears when the installed image differs. No QML or C++ code is hot-swapped.

Ctrl+K → Preview versions lists saved builds. Enter or clicking a build selects
it and gracefully reopens the preview. Sending, recording and transcription
disable restart actions. QSettings is synchronized before exit; the helper waits
for the old process to exit before launching, avoiding single-instance races.
Host agents and conversation history are not stopped or restored.

Rollback pins the chosen binary. Automatic checks do not fetch or rebuild while
pinned, even with --force. Latest / resume updates clears that pin. Known builds
show the semantic release version plus source commit; legacy backups without
reliable release metadata show a build hash and date instead.

The updater's `preview_versions.py` archives immutable binary copies and metadata
in `~/.local/lib/clarp-desktop-preview/versions/`; metadata and the pin are stored
in `history.sqlite`. Selection requires a known full hash, validates saved bytes,
uses the updater lock and checks the expected installed hash before replacement.
It never restores a historical Host database or silently rewinds user settings.
Older executables may not support newer preferences or installed Qt libraries.

Clarp Preview Versions in the app launcher invokes a retained capable build's
standalone picker, without a Host connection. This recovery route remains usable
when an older selected build lacks the picker. It selects the next installed
version but does not force-close an existing preview window. CLI equivalents:

```
clarp-desktop-preview-update --catalog
clarp-desktop-preview-update --manage
clarp-desktop-preview --versions
```

Tests: updater/history Python tests use temporary binaries, a real isolated
restart subprocess, corrupt-build and stale-selection guards, and pinned checks.
`tst_preview_versions.qml` verifies Enter/arrows/Escape and unsafe-state gating.
Existing pane-draft persistence tests remain part of full CTest. The screenshot
scenario `preview-versions` uses synthetic version metadata and cannot install
or restart. Capture its banner and `previewVersionPanel` overlay headlessly.
