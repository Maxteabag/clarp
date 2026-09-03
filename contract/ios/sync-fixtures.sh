#!/bin/sh
# Copy the golden fixtures into the iOS repo for CoreBehaviorTests.
# Usage: contract/ios/sync-fixtures.sh [dest-dir]
# Default dest is the sibling clarp-ios checkout's fixture folder.
set -eu
here=$(dirname "$0")
dest=${1:-"$here/../../../clarp-ios/ClarpNative/ContractFixtures"}
mkdir -p "$dest"
cp -r "$here/../fixtures/." "$dest/"
echo "synced $(find "$dest" -name '*.json' | wc -l) fixtures to $dest"
