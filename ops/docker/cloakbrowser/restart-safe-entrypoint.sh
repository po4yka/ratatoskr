#!/bin/bash
set -euo pipefail

# The container process namespace is fresh after every restart, but its
# writable layer is not. Remove Xvfb state that can only belong to the previous
# process namespace before the upstream entrypoint starts display :99 again.
rm -f -- /tmp/.X99-lock /tmp/.X11-unix/X99

exec /entrypoint.sh "$@"
