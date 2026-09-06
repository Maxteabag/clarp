# Stage two — shipped changes

Private HTML walkthrough: https://elitebook.tailf14237.ts.net:12443/changes/

`index.html` is self-contained: actual before/after screenshots, a recorded
thumbnail, inline renderer modules and styles. It can be downloaded and reopened
without fetching images or libraries. Navigation to the live map requires network.
It is explanatory material, not a planning form; it collects and submits nothing.

The interactive guide uses the shipped `8c38b99` lantern code with explicitly
illustrative state data. It is not a running live agent scene. Deployed replay
captures and the isolated development preview are labeled separately.

Rebuild using `build.py` (Python + ImageMagick + repository Git history), passing
`--before`, `--after`, `--unresolved` and `--preview` with the full evidence image
paths if the original temporary captures are no longer present. `--renderer-ref`
defaults to the documented release. Updating the renderer requires updating the
release description to match. Complete screenshots are converted to JPEG without
cropping; the generated HTML retains its own copies after captures are removed.

Publish index.html to `/home/peter/Documents/Clarp/fleet-map-stage-two-changes/`.
The private Tailscale Serve /changes subpath is separate from /viz and /stage-two.
Verify with `node scripts/viz_changes_check.mjs URL OUTPUT_DIRECTORY`, then open
the screenshots for every state. Check the seven illustrative renderings, mobile
and desktop layout, comparison buttons, keyboard/image enlargement, and download.
