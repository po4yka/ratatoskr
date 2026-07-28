#!/bin/sh
set -eu

test -S /run/ratatoskr-dbus/system_bus_socket
dbus-send \
    --bus=unix:path=/run/ratatoskr-dbus/system_bus_socket \
    --type=method_call \
    --print-reply \
    --reply-timeout=3000 \
    --dest=org.bluez \
    / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects \
    >/dev/null
