#!/bin/sh
set -eu

bus_socket=${AI_BACKUP_WEBAUTHN_DBUS_SOCKET:-/run/ratatoskr-dbus/system_bus_socket}

test -S "$bus_socket"
managed_objects=$(dbus-send \
    --bus="unix:path=${bus_socket}" \
    --type=method_call \
    --print-reply \
    --reply-timeout=3000 \
    --dest=org.bluez \
    / \
    org.freedesktop.DBus.ObjectManager.GetManagedObjects)

adapter_paths=$(printf '%s\n' "$managed_objects" | awk '
    /object path "/ {
        path = $3
        gsub(/"/, "", path)
    }
    /string "org[.]bluez[.]Adapter1"/ {
        print path
    }
')
test -n "$adapter_paths"

for adapter_path in $adapter_paths; do
    if adapter_powered=$(dbus-send \
        --bus="unix:path=${bus_socket}" \
        --type=method_call \
        --print-reply \
        --reply-timeout=3000 \
        --dest=org.bluez \
        "$adapter_path" \
        org.freedesktop.DBus.Properties.Get \
        string:org.bluez.Adapter1 \
        string:Powered) \
        && printf '%s\n' "$adapter_powered" | grep -Eq \
            'variant[[:space:]]+boolean[[:space:]]+true'; then
        exit 0
    fi
done

exit 1
