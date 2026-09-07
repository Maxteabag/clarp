# Third-party notices

Clarp Desktop dynamically links against the open-source Qt 6 libraries named
in `CMakeLists.txt`. The project does not modify Qt. Qt's applicable
open-source license texts and source-code offers are published by the Qt
Project at <https://www.qt.io/licensing/open-source-lgpl-obligations> and
<https://code.qt.io/>.

Linux packages may also use FFmpeg, GStreamer, PulseAudio/PipeWire, OpenSSL,
and system graphics libraries through Qt or the operating-system runtime.
Their precise set and license versions depend on the selected distribution
format. Flatpak obtains these from `org.kde.Platform`; the AUR package uses
system packages; the AppImage bundles its resolved runtime libraries.

Before publishing an AppImage, inspect its final dependency inventory and ship
the corresponding license texts. The build recipe copies Qt's LGPL text when
the build SDK exposes it under `/usr/share/licenses/qt6-base/`.
