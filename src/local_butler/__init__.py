"""Local Butler — shared entry point and app launcher."""

import sys


APPS = {
    "money": {
        "module": "local_butler.apps.money_tracker.main:app",
        "port": 8787,
    },
    "geonews": {
        "module": "local_butler.apps.geonews.main:app",
        "port": 8788,
    },
    # Calendar wraps Radicale in a GTK3 window; it is not an ASGI app, so it
    # cannot be handed to uvicorn. It is a self-contained script needing
    # PyGObject, which lives in the *system* interpreter rather than this
    # venv, so it is dispatched as an explicit interpreter + script pair.
    "calendar": {
        "interpreter": "/usr/bin/python3",
        "script": "/home/bpoppa12/local-butler/src/local_butler/apps/calendar/main.py",
        "gui": True,
    },
}


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: local-butler <app>")
        print(f"Available apps: {', '.join(APPS)}")
        print("Note: Geonews native GTK app is launched via ~/.local/bin/geonews")
        sys.exit(1)

    app_name = args[0]
    if app_name not in APPS:
        print(f"Unknown app: {app_name}. Available: {', '.join(APPS)}")
        sys.exit(1)

    config = APPS[app_name]
    rest = args[1:]

    if "script" in config:
        import subprocess

        sys.exit(
            subprocess.run(
                [config.get("interpreter", sys.executable), config["script"], *rest]
            ).returncode
        )

    import uvicorn

    uvicorn.run(
        config["module"],
        host="127.0.0.1",
        port=config["port"],
        reload=False,
    )


if __name__ == "__main__":
    main()
