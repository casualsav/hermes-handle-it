"""Mirror a tmux pane into a web page (ANSI colours kept), so a screen recording can show
Hermes's terminal beside the browser it drives.

    python term_mirror.py <tmux-target> [--port 8766]
"""

import argparse
import html
import http.server
import re
import subprocess

PAGE = """<!doctype html><meta charset=utf-8><title>Hermes</title>
<style>body{margin:0;background:#0c0f14;color:#d6dde6}
pre{margin:0;padding:10px 12px;font:12.5px/1.32 'DejaVu Sans Mono',monospace;white-space:pre}</style>
<pre id=t></pre><script>
async function tick(){try{const r=await fetch('/screen');document.getElementById('t').innerHTML=await r.text()}catch(e){}
setTimeout(tick,250)}tick()</script>"""

BASIC = ["#1c1f24", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d6dde6",
         "#5c6370", "#ff7b86", "#b5e890", "#ffd68a", "#82c4ff", "#de9bf0", "#7fd6e0", "#ffffff"]


def _xterm(n: int) -> str:
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def ansi_to_html(text: str) -> str:
    out, style = [], {}
    pos = 0
    for m in re.finditer(r"\x1b\[([\d;]*)m", text):
        out.append(_span(text[pos:m.start()], style))
        pos = m.end()
        codes = [int(c) if c else 0 for c in m.group(1).split(";")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                style = {}
            elif c == 1:
                style["font-weight"] = "bold"
            elif c == 2:
                style["opacity"] = ".7"
            elif c == 3:
                style["font-style"] = "italic"
            elif c in (22, 23):
                style.pop("font-weight" if c == 22 else "font-style", None)
                style.pop("opacity", None)
            elif 30 <= c <= 37 or 90 <= c <= 97:
                style["color"] = BASIC[c - 30 if c < 90 else c - 82]
            elif 40 <= c <= 47 or 100 <= c <= 107:
                style["background"] = BASIC[c - 40 if c < 100 else c - 92]
            elif c == 39:
                style.pop("color", None)
            elif c == 49:
                style.pop("background", None)
            elif c in (38, 48) and i + 1 < len(codes):
                key = "color" if c == 38 else "background"
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    style[key] = _xterm(codes[i + 2])
                    i += 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    style[key] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                    i += 4
            i += 1
    out.append(_span(text[pos:], style))
    return "".join(out)


def _span(chunk: str, style: dict) -> str:
    chunk = html.escape(re.sub(r"\x1b\[[\d;?]*[A-Za-z]", "", chunk))
    if not chunk or not style:
        return chunk
    css = ";".join(f"{k}:{v}" for k, v in style.items())
    return f'<span style="{css}">{chunk}</span>'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/screen":
                raw = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-t", args.target],
                                     capture_output=True, text=True).stdout
                body = ansi_to_html(raw).encode()
            else:
                body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
