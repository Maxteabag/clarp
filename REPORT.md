# perf-forward report

## What changed

- Added a Qt-free public `clarp-desktop` launcher in `desktop/src/launcher/`.
- Renamed the Qt application target/output to `clarp-desktop-real`; the launcher
  forwards over the existing instance socket and `exec`s `clarp-desktop-real`
  when no listener answers.
- Moved socket identity and forwarding wire protocol into shared non-Qt code
  used by both launcher and `InstanceServer`.
- Added launcher tests:
  - native socket identity/forwarding protocol coverage in `clarp-desktop-core`
  - `clarp-desktop-launcher-exec-fallback`
  - `clarp-launcher-real-probe`
- Added lab-only `desktop/tools/second-launch-real-aware.sh` so the owned
  harness can measure a launcher that becomes `clarp-desktop-real`.

Commits on `perf/perf-forward` after rebasing onto `origin/main`
`d06119f44dd7ffc9803ca78f1b9ac270db7ef174`:

- `537f6d3 WIP: add tiny desktop forwarding launcher`
- `e576e2a WIP: clean launcher analysis warnings`
- `fa00c53 WIP: clean launcher probe analysis warning`
- `899cef3 WIP: add real-aware launcher lab probe`
- `8fc5eae WIP: pass lab env through launcher probe`

## XPS measurement

Recorded in perf-loop as `it-0046-qt-free-forwarded-launcher-xps-ssd-abba`.

Raw evidence:
`~/.local/share/clarp/perf-loop/raw/2026-09-27-perf-forward-xps-ssd-abba.txt`

Host/cohort:

- `xps15`
- `xps-ssd`
- `LAB_ENV=QT_WAYLAND_DISABLE_WINDOWDECORATION=1`
- owned harness only, refreshed from `/var/tmp/clarp-xpslab/tools`
- 7 ABBA blocks, 14 accepted before and 14 accepted after samples
- accepted bench-start load range: 1.14 to 1.97
- skipped high-load attempts: 6

Binaries:

- before commit: `d06119f44dd7ffc9803ca78f1b9ac270db7ef174`
- after local commit: `8fc5eae`
- after XPS applied-patch commit: `542b717d6774437a241e344e7de844a1660c1767`
- before `clarp-desktop`: `c099c40d99ed3857e9f0298ec19c06f8b826395278393b1dcd70258867954061`
- after launcher `clarp-desktop`: `f08a0c7327ffa4a18abd7371f16f1af4ee4bb3266a5fe1e3c4d7c909130bd635`
- after real app `clarp-desktop-real`: `19396f7a2b15765e46460abe82ed7c8ffc9e0a07da05c85debd8f878f2080397`

Results:

| Metric | Before | After |
| --- | ---: | ---: |
| forwarding client exit p50 | 32 ms | 4 ms |
| forwarding client exit mean | 33.86 ms | 4.57 ms |
| forwarding client exit range | 30-47 ms | 4-7 ms |
| server request to first frame p50 | 30 ms | 28.5 ms |
| server request to first frame mean | 33 ms | 31.36 ms |
| added PSS per forwarded window | 6 MB | 6 MB |

Conclusion: the lever is a real win for the launching process. It removes about
28 ms p50 from forwarded launch client lifetime on XPS without adding a second
resident process. The server-side new-window paint path is effectively neutral.

## Gates

- XPS release builds: passed for before and after.
- Local `cmake --build --preset dev -j4`: completed; later no-op confirmed.
- Local full `ctest --preset dev -j4 --output-on-failure`: failed 6/30 QML
  timing/layout tests under offscreen idle scheduling.
- Rerun failed set at `-j1`: the same 6 failed again:
  `activity-layout`, `settings-keyboard`, `context-keyboard`,
  `transcript-scroll`, `transcript-switch`, `ready-reply`.
- Native launcher/core tests passed in the full run, including
  `clarp-desktop-core`, `clarp-desktop-native-only`, and
  `clarp-desktop-launcher-exec-fallback`.
- `qmllint`: not applicable; no QML files changed.
- `cmake --build --preset analysis -j4`: passed under SCHED_IDLE. No
  clang-tidy flag-filter block occurred on this branch.

## What to ship

Ship the launcher split after the gate/package items below are resolved:

- public entry point remains `clarp-desktop`
- Qt app binary is `clarp-desktop-real` next to it
- CMake installs both binaries
- socket/protocol behavior stays shared between launcher and `InstanceServer`

## What not to ship yet

- Do not ship an installer/update that copies only `clarp-desktop`; it will
  install the launcher without its sibling real app.
- Do not ship an AppImage that scans dependencies only from the launcher; the
  launcher has no Qt dependencies, so Qt libraries must be discovered from
  `clarp-desktop-real`.
- Do not treat the lab helper as production code. It is only for owned-harness
  measurement of a launcher/real-app split.
- Do not claim full gates are green until the QML test failures are explained,
  fixed, or explicitly waived by Lena.

## Packaging follow-up

Smallest packaging changes I would make next:

- `~/.local/lib/clarp-desktop-preview/install-preview.py`: copy/stage/backup
  both `clarp-desktop` and `clarp-desktop-real`, and include both hashes in the
  receipt/build-info.
- `~/.local/lib/clarp-desktop-preview/auto_update.py` and
  `clarp-desktop-preview-update`: update rollback and provenance checks for the
  two-binary install.
- AppImage build: include `clarp-desktop-real` as an executable/dependency scan
  root while keeping the desktop entry pointed at `clarp-desktop`.
- CI artifact upload should include both binaries, or an archive containing both.

## Next

Resolve the existing QML gate failures or get Lena's waiver, then integrate the
packaging changes and re-run Lena's measurement from the install artifact.
