# Clarp-managed agent toolchain

This directory is the reviewed source of truth for managed Claude Code and
Codex versions. Clarp does not fork either vendor CLI.

- `toolchain.json` pins the Node runtime and SHA-256 for every supported
  macOS/Linux architecture.
- `package.json` pins exact vendor package versions.
- `package-lock.json` pins their transitive/platform packages.

The installer hashes these files, installs into
`<data>/toolchains/<hash>/`, and records the absolute directory in each Clarp
release. Updating Clarp may install a new hash; rollback continues using the
old toolchain. Vendor credentials remain in their normal user-owned homes.

To update intentionally:

1. Change the exact package versions.
2. Run `npm install --package-lock-only --ignore-scripts` here.
3. If Node changes, update every archive/checksum in `toolchain.json` from the
   official Node release checksum manifest.
4. Run the toolchain installer tests and a disposable real install proof.
5. Review vendor release notes before committing.
