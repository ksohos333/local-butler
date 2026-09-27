# Local Butler Cloud — Multi-Backend Hub

> Standalone GTK app that manages multiple local-first cloud/sync options on a single machine.

## Supported backends

| Backend | What it is | Managed by the app |
|---------|------------|--------------------|
| **Syncthing** | Peer-to-peer continuous sync | Start/stop service, view device ID, open Web UI, open default folder |
| **MinIO** | S3-compatible object storage | Start/stop service if `minio` is installed |
| **rclone** | Sync/mount/fetch from many cloud providers | Open config terminal, list remotes, open docs |
| **Nextcloud** | Full self-hosted cloud stack | Detect podman/docker, show deploy command |
| **ownCloud** | Lighter self-hosted files/calendar/contacts | Detect podman/docker, show deploy command |
| **Seafile** | Fast file library sync server | Detect podman/docker, show deploy command |
| **Samba** | Windows-compatible LAN sharing | Show install/running status |
| **NFS** | Unix-style LAN sharing | Show install/running status |

## Design philosophy

- **Local-first**: data stays on hardware you control.
- **No external cloud required**: Syncthing works LAN-only; MinIO/rclone serve data locally.
- **Fetch options**: rclone can pull data from external providers into local storage if you choose.
- **Detection over assumption**: the app only enables controls for backends that are actually installed.

## Files

- `~/local-butler/src/local_butler/apps/cloud/main.py` — app source.
- `~/.local/bin/local-butler-cloud` — launcher.
- `~/.local/share/applications/local-butler-cloud.desktop` — desktop shortcut.
- `~/.config/systemd/user/local-butler-syncthing.service` — Syncthing service.
- `~/.config/systemd/user/local-butler-minio.service` — created on first MinIO start.
- `~/.config/syncthing-local-butler/` — isolated Syncthing config.
- `~/.local/share/local-butler-cloud/minio/` — MinIO data directory.

## Adding a new backend

1. Subclass `CloudBackend` in `main.py`.
2. Implement `detect()`, `is_running()`, optional `start()`/`stop()`, `status_lines()`, `setup_hint()`, and `actions()`.
3. Add an instance to the `BACKENDS` list.

## Security notes

- Syncthing and MinIO bind to `127.0.0.1` by default.
- MinIO root credentials default to `localbutler` / `localbutler`; change them in production.
- Samba/NFS are system-level services and cannot be started from a user app without `pkexec`; the app only reports status.
