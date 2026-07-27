#!/bin/sh
set -eu

test -S /tmp/.X11-unix/X99
pgrep -x Xtigervnc >/dev/null
pgrep -x openbox >/dev/null
awk '$2 ~ /:170C$/ && $4 == "0A" { found = 1 } END { exit !found }' /proc/net/tcp
