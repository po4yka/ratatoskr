#!/bin/sh
set -eu

host_bus=/run/host-dbus/system_bus_socket
proxy_dir=/run/ratatoskr-dbus
proxy_bus=${proxy_dir}/system_bus_socket

test -S "$host_bus"
mkdir -p "$proxy_dir"
rm -f "$proxy_bus"

# The browser needs BlueZ for WebAuthn hybrid transport (caBLE), but must not
# inherit access to every privileged service on the host system bus. The output
# socket is shared only with the matching provider's re-auth browser container.
umask 000
exec xdg-dbus-proxy "unix:path=${host_bus}" "$proxy_bus" \
    --filter \
    --talk=org.bluez
