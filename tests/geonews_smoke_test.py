"""End-to-end API smoke test for Geonews markets.

Safety rules (this script used to delete the production database):

* It NEVER touches ``~/.local/share/local-butler/butler.db``. Every run uses a
  throwaway HOME, so the app resolves its database inside a temp directory.
* It refuses to run while port 8788 is occupied. ``geonews.service`` owns that
  port, and starting a second server here would fight it for the bind.
* It creates a market and uses the id the API returns instead of assuming id 1.

Run it with the service stopped, e.g.::

    systemctl --user stop geonews.service
    .venv/bin/python tests/geonews_smoke_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path("/home/bpoppa12/local-butler")
VENV_BIN = REPO_ROOT / ".venv" / "bin"
PROD_DB = Path.home() / ".local" / "share" / "local-butler" / "butler.db"
HOST = "127.0.0.1"
PORT = 8788
BASE = f"http://{HOST}:{PORT}"


def port_in_use(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex((host, port)) == 0


def request(
    method: str, path: str, data: dict | None = None
) -> tuple[int, dict | str]:
    url = BASE + path
    payload = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=payload, method=method)
    if payload:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def wait_for_server(timeout: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(BASE + "/", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    if port_in_use():
        print(
            f"REFUSING TO RUN: {HOST}:{PORT} is already in use.\n"
            "geonews.service owns that port. Stop it first:\n"
            "    systemctl --user stop geonews.service"
        )
        return 2

    prod_before = PROD_DB.stat().st_size if PROD_DB.exists() else None

    # A throwaway HOME keeps alembic and the app on a private database.
    tmp_home = Path(tempfile.mkdtemp(prefix="geonews-smoke-"))
    env = os.environ.copy()
    env["HOME"] = str(tmp_home)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    private_db = tmp_home / ".local" / "share" / "local-butler" / "butler.db"
    print(f"using throwaway HOME: {tmp_home}")
    print(f"database will be   : {private_db}")

    proc = None
    try:
        print("applying migrations to the throwaway database...")
        subprocess.run(
            [str(VENV_BIN / "alembic"), "upgrade", "head"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

        proc = subprocess.Popen(
            [
                str(VENV_BIN / "uvicorn"),
                "local_butler.apps.geonews.main:app",
                "--host",
                HOST,
                "--port",
                str(PORT),
                "--no-access-log",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        print("waiting for server...")
        if not wait_for_server(timeout=30):
            print("server did not become ready")
            return 1
        print("server ready")

        status, created = request(
            "POST", "/api/markets", {"title": "Will the Fed cut rates in Q4 2026?"}
        )
        print(f"\n=== create market ===\nHTTP {status}")
        print(json.dumps(created, indent=2) if isinstance(created, dict) else created)
        if status >= 400:
            print("FAILED: create market")
            return 1

        market_id = created["id"] if isinstance(created, dict) else 1

        checks = [
            ("list markets", lambda: request("GET", "/api/markets")),
            ("portfolio before", lambda: request("GET", "/api/markets/portfolio/me")),
            (
                "place bet",
                lambda: request(
                    "POST", f"/api/markets/{market_id}/bets", {"side": "yes", "shares": 50}
                ),
            ),
            ("portfolio after", lambda: request("GET", "/api/markets/portfolio/me")),
            (
                "settle yes",
                lambda: request(
                    "POST", f"/api/markets/{market_id}/settle?outcome=yes"
                ),
            ),
            ("portfolio final", lambda: request("GET", "/api/markets/portfolio/me")),
        ]

        for name, fn in checks:
            status, body = fn()
            print(f"\n=== {name} ===")
            print(f"HTTP {status}")
            print(json.dumps(body, indent=2) if isinstance(body, dict) else body)
            if status >= 400:
                print(f"FAILED: {name}")
                return 1

        print("\nSMOKE TEST PASSED")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                if stream:
                    tail = stream.read()[-1500:]
                    if tail:
                        print(f"\n=== server {label} tail ===\n{tail}")

        if private_db.exists():
            print(f"\nthrowaway database used: {private_db} "
                  f"({private_db.stat().st_size} bytes)")
        shutil.rmtree(tmp_home, ignore_errors=True)

        prod_after = PROD_DB.stat().st_size if PROD_DB.exists() else None
        if prod_before == prod_after:
            print(f"production database untouched ({PROD_DB})")
        else:
            print(f"WARNING: production database changed size "
                  f"{prod_before} -> {prod_after}")


if __name__ == "__main__":
    sys.exit(main())
