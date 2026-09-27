# Game Compatibility Checker

> Open-source game requirements checker integrated into Local Butler Diagnostic (Gaming tab).

## How it works

Unlike proprietary sites such as "Can You RUN It?", this checker uses the **public Steam Store API** directly:

- Fetches `pc_requirements` for a given Steam app ID.
- Strips HTML and extracts RAM, disk space, and Windows OS requirements.
- Compares those numbers to local hardware detected from `/proc/cpuinfo`, `/proc/meminfo`, `lspci`, and `shutil.disk_usage`.
- Reports **OK / WARN / FAIL / ERROR** with a short explanation.

No external proprietary service, browser plugin, or account is required.

## Current limitations

- CPU and GPU comparisons are informational only; the checker does not yet compare model generations.
- Requirements parsing is heuristic; some games may report requirements in unusual formats.
- The bundled ProtonDB seed covers popular titles; lesser-known games fall back to a generic Proton-installed warning.

## Usage

1. Open **Local Butler Diagnostic**.
2. Click the **Gaming** tab.
3. Pick an installed Steam game from the dropdown, or type a Steam app ID and click **Check ID**.

## Example

For **Hearts of Iron IV** (Steam app ID `394360`):

- Minimum: 4 GB RAM, 2 GB disk, Windows
- Recommended: 6 GB RAM, 2 GB disk
- ProtonDB rating: **Gold**
- On a Linux machine with plenty of RAM/disk and Proton installed: **OK** — Windows-only, but ProtonDB Gold and a Proton installation were detected.

## Files

- `~/local-butler/src/local_butler/apps/diagnostic/gaming.py` — checker and GTK panel.
- `~/local-butler/src/local_butler/apps/diagnostic/protondb.py` — bundled ProtonDB compatibility seed.
- Wired into `~/local-butler/src/local_butler/apps/diagnostic/main.py` as the **Gaming** tab.

## Future improvements

- Parse CPU/GPU model names and compare against a benchmark database.
- Cache Steam API responses to avoid repeated network calls.
- Add a `--refresh-protondb` command to update the bundled compatibility seed.
- Add GOG/Epic/itch.io game support if manifests are detectable.
