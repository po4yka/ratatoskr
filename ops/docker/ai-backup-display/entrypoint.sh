#!/bin/sh
set -eu

export DISPLAY=:99
export XDG_RUNTIME_DIR=/tmp/openbox-runtime

rm -f -- /tmp/.X99-lock /tmp/.X11-unix/X99
mkdir -p /tmp/.X11-unix "$XDG_RUNTIME_DIR"
chmod 1777 /tmp/.X11-unix
chmod 0700 "$XDG_RUNTIME_DIR"

Xtigervnc :99 \
  -geometry 1920x1080 \
  -depth 24 \
  -rfbport 5900 \
  -SecurityTypes None \
  -AlwaysShared \
  -AcceptKeyEvents \
  -AcceptPointerEvents \
  -AcceptCutText \
  -SendCutText \
  -localhost no \
  -nolisten tcp \
  -ac &
xvnc_pid=$!

cleanup() {
  kill "${openbox_pid:-}" "$xvnc_pid" 2>/dev/null || true
  wait "${openbox_pid:-}" "$xvnc_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

attempt=0
while [ ! -S /tmp/.X11-unix/X99 ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 100 ] || ! kill -0 "$xvnc_pid" 2>/dev/null; then
    echo "Xtigervnc failed to create display :99" >&2
    exit 1
  fi
  sleep 0.1
done

openbox --config-file /etc/xdg/openbox/ratatoskr-rc.xml &
openbox_pid=$!
wait "$xvnc_pid"
