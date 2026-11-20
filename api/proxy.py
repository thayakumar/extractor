"""GET /api/proxy?url=<image-url> → proxied image bytes (CORS bypass)."""

import urllib.request
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        url = qs.get("url", [None])[0]

        if not url or "anyflip.com" not in url:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad request")
            return

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "image/webp")
        except Exception:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"Upstream fetch failed")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)
