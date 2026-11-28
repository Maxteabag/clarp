#!/usr/bin/env bash
# Screenshots of the main screens in one language, for translation review:
# slint-app/tests/screens.sh LANG OUTDIR [THEME]
# LANG is en, nb, zh-Hans, hi, es, fr (or system). Each screen is one headless
# run of tests/shot.sh against the fake Host, opened by the command named here.
# The fake Host runs with a full chat list (--roster) and a worked turn in
# Rachel's chat (--rich: tool rows, a failed step, a job, a decision).
set -uo pipefail
cd "$(dirname "$0")/../.."
lang=${1:?language}; out=${2:?output folder}; theme=${3:-terminal}
mkdir -p "$out"
export CLARP_FAKE_HOST_ARGS="--roster 12 --rich"
status=0
# name:command (empty command: the chat itself)
screens=(
    chat:                     # chat list with previews, a turn with tool rows, the composer
    settings:settings         # settings catalogue
    switcher:switcher         # Ctrl+K command palette
    keys:help-keys            # key help (?)
    keymap:edit-keymap        # key-binding editor
    updates:updates           # updates feed (artifacts, decisions)
    teams:teams               # teams page
    overview:overview         # all agents overview
    processes:agent-processes # processes / jobs
    profile:agent-profile     # agent profile
    orchestrator:orchestrator # orchestrator dialog
    connection:connection     # connection page
    new:new                   # new agent hub
    queue:manage-queue        # queued messages
    font:choose-font          # font and reading theme picker
)
for screen in "${screens[@]}"; do
    name=${screen%%:*}; command=${screen#*:}
    CLARP_LANGUAGE="$lang" CLARP_SHOT_COMMAND="$command" slint-app/tests/shot.sh "$out/$name-$lang.png" rachel "$theme" || status=1
done
exit $status
