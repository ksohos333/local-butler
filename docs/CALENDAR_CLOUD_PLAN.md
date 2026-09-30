# Local Calendar & Cloud Plan

> Brainstorm and implementation plan for local-first calendar/event management and local cloud storage on the user's Rocky Linux 9 workstation.

---

## A. Calendar / Event Manager

> **Status: shipped.** The scheduling described below is now implemented. See
> [CALENDAR_SCHEDULING.md](CALENDAR_SCHEDULING.md) for how it actually works.
> The survey below is kept as the original reasoning, with the machine state
> corrected where it is now out of date.

### Current state on the machine
- **GNOME Calendar** is now *installed* at `/usr/bin/gnome-calendar`.
- **Evolution** and **Thunderbird** are still *not* installed.
- **Radicale** (a small CalDAV/CardDAV server) is installed at `/usr/bin/radicale`,
  runs as a systemd user service on `127.0.0.1:5232`, and is enabled at boot.
- The `local/calendar` collection exists and holds the `.ics` files.
- Other CLI calendars (`khal`, `todoman`, `calcurse`, `remind`) are not present.
- **Syncthing** binary exists at `/home/bpoppa12/bin/syncthing` with a web UI desktop shortcut.

### Recommended architecture: Local CalDAV + any client

#### Server side (already available)
- **Radicale** — lightweight CalDAV/CardDAV server that stores calendars and contacts as local `.ics`/`.vcf` files.
  - No external dependency or cloud.
  - Authentication can be local or disabled for single-user use.
  - Files live under `~/.var/lib/radicale/collections` or a custom directory.
  - Can be exposed only on `localhost` or a local Unix socket for security.

#### Client options to evaluate
| Client | Type | Pros | Cons |
|--------|------|------|------|
| **GNOME Calendar** | GUI | Native GNOME, notifications, pretty views | Installed; needs Evolution Data Server for CalDAV sync |
| **Evolution** | GUI E-mail/calendar | Full PIM, CalDAV support, tasks, reminders | Heavy; not installed |
| **Thunderbird + Lightning** | E-mail/calendar | Already on many systems, CalDAV capable | May not be installed |
| **calcurse** | TUI | Very lightweight, local `.conf` storage | No CalDAV without scripts |
| **khal** | CLI + TUI | CalDAV-friendly with `vdirsyncer` | Needs pip install |
| **Standard Notes / Joplin** | Notes+calendar plugins | Encrypted local option | Calendar plugins are limited |

### Recommended minimal setup
1. **Start Radicale as a systemd user service** on `127.0.0.1:5232`.
2. **Configure Radicale** to use filesystem storage under the user's home directory.
3. **Install GNOME Calendar + evolution-ews/evolution-data-server** (or the smaller `gnome-calendar` + `evolution-data-server`) if you want a native GUI.
4. **Add the local CalDAV account** in GNOME Calendar pointing to `http://127.0.0.1:5232/<user>/<calendarname>/`.
5. **Back up calendar files** with the existing Syncthing setup or a simple `rsync`.

### Calendar integration for Local Butler (next phase)
- Add a **Calendar** tab in the Diagnostic app that:
  - Tests whether Radicale, GNOME Calendar, or Evolution is installed/running.
  - Starts/stops the local Radicale service.
  - Shows the number of events in local `.ics` files.
  - Checks that the CalDAV URL is reachable.

---

## B. Local Cloud Storage Alternatives to Google Cloud / OneDrive

### Existing pieces on the machine
- **Syncthing** binary exists — peer-to-peer continuous file sync.
- **Radicale** exists — local contacts/calendar sync.
- **Flatpak** is available for sandboxed apps.

### Decision matrix

| Solution | Architecture | Encryption | External dependency | Best for |
|----------|--------------|-----------|---------------------|----------|
| **Syncthing** | P2P between your devices | TLS in transit + optional password for shares on untrusted nodes | None — no central server | Folder sync across your own machines / phones |
| **Nextcloud** (self-hosted) | Full LAMP stack on local machine | Server-side encryption options + HTTPS | None if on localhost | Files, calendar, contacts, notes, app ecosystem |
| **ownCloud** (Community/Server) | Similar to Nextcloud, lighter core | Encryption plugin available | None if on localhost | Simpler self-hosted files/cal/contacts |
| **Seafile** | Dedicated file sync server + web UI | Encrypted libraries | None if on localhost | Large file libraries, fast sync |
| **MinIO** | S3-compatible object store on local machine | TLS + bucket policies | None | Local S3 API for apps/developers |
| **rclone mount** | Mounts remote/local backends as FUSE | Encrypted remote with `rclone crypt` | Depends on configured backend | Local-encrypted layer on top of USB/NAS |
| **LUKS-encrypted USB/NAS** | Block-device encryption | Full disk | None | Cold/offline storage of sensitive data |

### Recommended stack for privacy + control
1. **Day-to-day device sync** → **Syncthing** (already present)
   - Run as `syncthing --no-browser` + systemd user service.
   - Share only the folders you want synced.
   - No cloud provider can see your files.
2. **Rich cloud services (files + calendars + contacts)** → **Nextcloud** self-hosted locally
   - Install via Podman/Docker or native packages.
   - Bind to `127.0.0.1:8080` only — no external exposure.
   - Optionally expose through your own VPN/wireguard for remote access.
3. **S3-compatible local object store** → **MinIO**
   - Useful if you want apps to talk to an S3-like API without Amazon.
4. **Offline backups** → LUKS-encrypted USB drive + `rsync`

### Instant wins you can do now
1. Start Syncthing as a user service and add at least one shared folder (e.g. `~/Documents/local-cloud`).
2. Start Radicale from Local Butler to create a personal CalDAV endpoint.
3. Configure GNOME Calendar (after install) to read the local Radicale calendar.

### What to avoid for privacy
- **Dropbox / Google Drive / OneDrive / iCloud** → central servers outside your control.
- **Zoom Cloud / Slack Files** → same issue.
- Any service that requires upload to foreign datacenters.

### Cloud integration for Local Butler (next phase)
- Add a **Cloud** tab that:
  - Detects Syncthing, Nextcloud, ownCloud, Seafile, MinIO, rclone.
  - Shows whether each service is running.
  - Displays configured synced folders.
  - Provides one-click start/stop for Syncthing and Radicale.
  - Warns if any known cloud-sync agent (Dropbox, OneDrive, Google Drive) is running.

---

## C. Proposed implementation order

1. **Calendar phase**
   - Create systemd user service for Radicale.
   - Create a minimal Radicale config under `~/.config/radicale/config`.
   - Add a Local Butler Calendar tab: start/stop Radicale, list local `.ics` calendars, show event count.
   - Optionally install `gnome-calendar` and wire it to the local CalDAV URL.

2. **Cloud phase**
   - Turn the existing Syncthing binary into a systemd user service.
   - Add a Local Butler Cloud tab: show Syncthing status, configured folders, start/stop.
   - Optionally install Nextcloud in Podman with localhost-only binding.

3. **Tighter integration**
   - Let Local Butler’s AI chat query the status of calendar/cloud services.
   - Add alerts if a calendar service or cloud sync fails.
