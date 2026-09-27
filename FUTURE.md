# Local Butler — Future Phases

> This file holds long-term ideas that come up during usage so they are not lost.

## Phase: Calendar & Event Manager ✅
- Implemented with **Radicale** (CalDAV server on `127.0.0.1:5232`) and a standalone **Local Butler Calendar** GTK app.
- Default calendar available at `http://127.0.0.1:5232/local/calendar/` (user `local`, password `local`).
- Next step: install a CalDAV GUI client such as GNOME Calendar or Evolution.

## Phase: Open-Source Local Cloud Storage ✅
- Implemented with **Syncthing** running as `local-butler-syncthing.service` and a standalone **Local Butler Cloud** GTK app.
- Web UI pinned to `http://127.0.0.1:8384`.
- Default folder `~/Sync` created.

## Phase: Privacy App UI
- Build a dedicated Local Butler Privacy desktop app.
- Show live camera/microphone usage, running processes, background services, and one-click kill/stop.
- Add network-connection inspection and permission summaries.

## Phase: Zoom / Teams Secure Install
- If required by work/family calls, install Zoom/Teams in a sandboxed way.
- Prefer web client; if native needed, use Flatpak + network/clipboard restrictions.

## Phase: Diagnostic Expansion
- Add network diagnostics (ping, DNS, VPN status).
- Add Bluetooth device diagnostics.
- Add storage-health checks (SMART, btrfs/zfs status).

## Phase: Geonews OSINT / Direct Extraction
- Deploy RSS-Bridge on `127.0.0.1:4000` for walled-garden feeds (Truth Social, X/Twitter, Instagram).
- Evaluate **OpenClaw** as a sandboxed browser-automation agent for JS-heavy primary sources.
  - Must run in a dedicated, unprivileged user space / container.
  - Read-only scraping of public posts, no logins or credentials.
  - Output JSON consumed by Geonews ingestion pipeline.
  - Implement only after Phase 4a (NER, bias, entity extraction) and only if RSS/simpler scrapers are insufficient.

