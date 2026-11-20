"""GET /api/config?url=<anyflip-url> → book metadata + page hash list."""

import json
import re
import urllib.request
from http.server import BaseHTTPRequestHandler


def _parse_url(url):
    url = url.strip().rstrip("/")
    m = re.search(r"https?://[^/]*anyflip\.com/([^/]+/[^/]+)", url)
    if not m:
        return None
    return f"https://online.anyflip.com/{m.group(1)}"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        url = qs.get("url", [None])[0]
        if not url:
            self._json({"error": "Missing ?url= parameter"}, 400)
            return

        base = _parse_url(url)
        if not base:
            self._json({"error": "Not a valid AnyFlip URL"}, 400)
            return

        try:
            text = _fetch(f"{base}/mobile/javascript/config.js")
            m = re.search(r"var\s+htmlConfig\s*=\s*(\{.+\})\s*;?\s*$", text, re.DOTALL)
            if not m:
                self._json({"error": "Could not parse config.js"}, 500)
                return
            config = json.loads(m.group(1))
        except Exception as e:
            self._json({"error": f"Failed to fetch config: {e}"}, 500)
            return

        pages = config.get("fliphtml5_pages", [])
        meta = config.get("meta", {})
        hashes = [p["n"][0] for p in pages if p.get("n")]

        self._json({
            "title": meta.get("title", "flipbook"),
            "pageCount": meta.get("pageCount", len(pages)),
            "pageWidth": meta.get("pageWidth", 595),
            "pageHeight": meta.get("pageHeight", 842),
            "baseUrl": f"{base}/files/large",
            "pages": hashes,
        })

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
