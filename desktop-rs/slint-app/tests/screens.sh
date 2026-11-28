#!/usr/bin/env bash
# Screenshots of the main screens in one language, for translation review:
# slint-app/tests/screens.sh LANG OUTDIR [THEME]
# LANG is en, nb, zh-Hans, hi, es, fr (or system). Each screen is one headless
# run of tests/shot.sh against the fake Host, opened by the command named here.
set -uo pipefail
cd "$(dirname "$0")/../.."
lang=${1:?language}; out=${2:?output folder}; theme=${3:-terminal}
mkdir -p "$out"
status=0
# name:command (empty command: the chat itself)
for screen in chat: settings:settings switcher:switcher keys:help-keys updates:updates teams:teams; do
    name=${screen%%:*}; command=${screen#*:}
    CLARP_LANGUAGE="$lang" CLARP_SHOT_COMMAND="$command" slint-app/tests/shot.sh "$out/$name-$lang.png" rachel "$theme" || status=1
done
exit $status
