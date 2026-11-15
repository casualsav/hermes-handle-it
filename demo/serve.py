"""Serve the demo shop's help center on http://127.0.0.1:8765/.

    python demo/serve.py [--port 8765]

Add ?speed=0.2 to the URL to make the scripted agent answer faster. Other options, combinable:
?login=1 (or ?login=2fa) puts the chat behind a sign-in, ?prechat=1 asks for a name and email first,
and /widget.html serves the chat in an iframe behind a launcher, where ?cookies=1 covers the
launcher with a cookie banner.
"""

import argparse
import functools
import http.server
from pathlib import Path

SHOP = Path(__file__).resolve().parent / "shop"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SHOP))
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"Northwind demo help center: http://{args.host}:{args.port}/")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
