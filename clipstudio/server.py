"""Serve Clip Studio: `python -m clipstudio`, or the desktop app's bundled backend, which passes a free port."""
import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clipstudio", description="Serve Clip Studio.")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    parser.add_argument("--check", action="store_true", help="load the app and its pages, then exit; checks a build")
    args = parser.parse_args(argv)

    # Imported here so --help answers without opening the database.
    from .app import STATIC_DIR, app

    if args.check:
        missing = [p for p in ("index.html", "studio.html", "publish.html") if not (STATIC_DIR / p).is_file()]
        if missing:
            raise SystemExit(f"The build is missing pages: {', '.join(missing)} in {STATIC_DIR}")
        print(f"Clip Studio loads, with its pages in {STATIC_DIR}")
        return

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
