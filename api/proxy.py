"""GET /api/proxy?url=<image-url> → proxied image bytes (CORS bypass)."""

import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://online.anyflip.com/",
}


def _try_fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "image/webp")
        return data, content_type


def _fallback_url(url):
    """If /files/large/ fails, try /files/mobile/ (Lofter1 approach)."""
    if "/files/large/" in url:
        alt = url.replace("/files/large/", "/files/mobile/")
        if alt.endswith(".webp"):
            alt = alt[:-5] + ".jpg"
        return alt
    return None


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
            data, content_type = _try_fetch(url)
        except (urllib.error.HTTPError, Exception):
            fallback = _fallback_url(url)
            if fallback:
                try:
                    data, content_type = _try_fetch(fallback)
                except Exception as e:
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(f"Both URLs failed: {e}".encode())
                    return
            else:
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
