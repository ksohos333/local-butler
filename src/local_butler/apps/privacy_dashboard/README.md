# Privacy Dashboard

Monitors camera and microphone access on this Linux desktop from the system tray. Default-deny: any process that is not explicitly allowed triggers an alert with **Block** (default), **Allow**, **Ask**, and **Kill** options.

## What runs

- `/home/bpoppa12/.local/bin/local-butler-privacy-dashboard` — main Python daemon.
- `~/.config/systemd/user/privacy-dashboard.service` — systemd user service.
- Tray icon in the GNOME panel labelled **P**.

## How detection works

- **Camera** — uses `fuser -v /dev/video*` to find processes holding video devices.
- **Microphone** — parses `pw-dump` for `Stream/Input/Audio` nodes and resolves missing PIDs with `psutil`.

## Controls

| Action | Result |
|--------|--------|
| **Block** (default) | Adds the app to the denylist and terminates it if running. |
| **Allow** | Remembers the app as trusted. |
| **Ask** | Does nothing now; you will be asked again next time. |
| **Kill** | Terminates the current process and adds it to the denylist. |

## Configuration

`~/.config/local-butler/privacy-dashboard.json`

```json
{
  "allowlist": {},
  "denylist": {},
  "auto_kill": false,
  "poll_interval": 2.0
}
```

Set `auto_kill` to `true` to terminate unknown access attempts automatically. Off by default.

## Service commands

```bash
systemctl --user start privacy-dashboard.service
systemctl --user stop privacy-dashboard.service
systemctl --user status privacy-dashboard.service
```

## Logs

`~/.local/share/local-butler/logs/privacy-dashboard.log`
