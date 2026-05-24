---
title: "Incident: Pi dockerd stopped 2026-05-24 18:42–18:59"
date: 2026-05-24
status: resolved
duration_minutes: 17
cause: intentional — nvme-migrate.sh stopped docker as part of NVMe migration procedure
---

# Incident: Pi dockerd stopped 2026-05-24 18:42–18:59

## Summary

All Ratatoskr containers were down for approximately 17 minutes (18:42–18:59 +0400). Docker was stopped intentionally twice by the operator as part of running `nvme-migrate.sh`, a one-shot NVMe data migration script. This was not a crash or daemon bug. Docker was manually restarted at 18:59 via `sudo systemctl start docker`.

## Timeline (all times +0400)

| Time | Event |
|---|---|
| 18:42:22 | Operator ran `sudo systemctl stop docker.socket docker`. All compose stacks stopped cleanly (SIGTERM, graceful shutdown). |
| 18:42:43 | `Restart=always` in docker.service caused dockerd to restart automatically (PID 132243). Docker socket came back up. |
| 18:42:56 | Operator ran `sudo systemctl stop docker docker.socket containerd` to stop containerd as well, in preparation for checking NVMe mount usage. |
| 18:43–18:52 | Operator ran `fuser`, `lsof`, `du` against `/mnt/nvme`; performed SSH key hardening; edited `/etc/fstab` to add `nofail,x-systemd.device-timeout=30s` to the NVMe mount entry. Docker remained stopped. |
| 18:52:43 | Operator launched `nvme-migrate.sh` via `sudo systemd-run --unit=nvme-migrate`. Script logged "Stopping docker stack..." and "Disabling user lingering and terminating po4yka session..." |
| 18:52:48–18:52:56 | `user@1000.service` teardown killed `qdrant.service`, `obsidian-sync.service`, and session processes with SIGKILL (status=9/KILL). Docker was already stopped at this point. |
| 18:52:59–18:53:52 | Script ran `fuser`/`lsof` to verify `/mnt/nvme` was free; found residual `zsh`, `pipewire`, `wireplumber`, `obsidian-sync.sh` processes still holding the mount. |
| 18:53:52 | Script began tar backup of `/mnt/nvme` (157 GB) to `/mnt/backup` (USB, 715 GB free). |
| 18:59:36 | Operator ran `sudo systemctl status docker` — confirmed docker was still stopped. |
| 18:59:42 | Operator ran `sudo systemctl start docker`. dockerd started (PID 135268). |
| 19:00:02–19:00:13 | Ratatoskr containers (postgres, redis, bot, worker, mobile-api, scheduler) reconnected to networks and came back online via `restart: always`. |

## Root Cause

Docker was deliberately stopped by the operator, not killed by the kernel or an external agent. The sequence:

1. `sudo systemctl stop docker.socket docker` at 18:42:22 — manual stop for NVMe prep work.
2. `sudo systemctl stop docker docker.socket containerd` at 18:42:56 — second stop to include containerd.
3. `nvme-migrate.sh` at 18:52:43 — script explicitly stopped the docker stack as its first step, then killed the user session (`loginctl terminate-user po4yka` or equivalent) to free the NVMe mount.

`docker.service` has `Restart=always` and `RestartSec=2`. A `systemctl stop` suppresses auto-restart (systemd sets `ActiveState=inactive` with `Result=success`, which does not trigger the restart policy). This is expected behavior — `Restart=always` only fires on non-clean exits, not on explicit operator stops.

## What Was Ruled Out

- **OOM killer**: `dmesg` showed no `oom_kill_process` or "Out of memory" entries in the outage window. Memory at investigation time: 3.9 GiB used / 15 GiB total, 257 MiB swap used.
- **Disk full**: `/mnt/nvme` at 41% (175 GiB / 458 GiB), root at 12% (26 GiB / 234 GiB), `/var/log` (log2ram) at 80% (102 MiB / 128 MiB) — elevated but not full at outage time.
- **Watchtower auto-update**: watchtower container not running at investigation time; no watchtower logs in the outage window.
- **Kernel / hardware fault**: No kernel errors in the outage window beyond routine Wi-Fi (`brcmf`) and UFW block log entries, which are chronic and pre-date this event.
- **containerd crash**: containerd stopped cleanly via explicit `systemctl stop` command.

## Residual Notes

The `/var/log` partition (log2ram, 128 MiB) was at 80% capacity at investigation time. If it fills to 100%, journald will start dropping log entries, which would make future incident analysis harder. Worth monitoring.

`nvme-migrate.sh` was placed at `/usr/local/sbin/nvme-migrate.sh` during the session, executed once, and then removed (confirmed by `ls` at investigation time returning "No such file or directory"). The migration log at `/var/log/nvme-migrate.log` captured the script's output. The tar backup was running at the end of the captured log window — its completion status was not recorded.

## Action Items

None required for this incident — the outage was planned operator maintenance. If future NVMe maintenance is needed, consider pre-announcing the window so the downtime is not mistaken for a failure.

If the `/var/log` (log2ram) partition approaches 95%, consider increasing `SIZE=` in `/etc/log2ram.conf` or enabling journald compression (`Compress=yes` in `/etc/systemd/journald.conf`).

## Evidence Files

- `sudo journalctl -u docker.service --since "2026-05-24 18:30" --until "2026-05-24 19:05"` — full docker stop/start sequence with PID transitions.
- `sudo journalctl --since "2026-05-24 18:52:43" -p warning` — SIGKILL events for qdrant, user@1000, obsidian-sync at 18:52:56.
- `/var/log/nvme-migrate.log` — migration script output (1827 bytes, last modified 18:53).
- `sudo journalctl --since "2026-05-24 18:42" | grep sudo` — full operator command audit trail.
