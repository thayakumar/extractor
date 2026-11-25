"""GET /api/config?url=<anyflip-url> → book metadata + page hash list."""

import json
import re
import urllib.request
from http.server import BaseHTTPRequestHandler


def _parse_url(url):
    url = url.strip().rstrip("/")
    url = re.sub(r"/mobile(/index\.html)?$", "", url)
    m = re.search(r"https?://[^/]*anyflip\.com/([^/]+/[^/]+)", url)
    if not m:
        return None
    return f"https://online.anyflip.com/{m.group(1)}"


def _fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_json(text):
    """Extract the JSON object from config.js — find first { to matching }."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _get_page_count(config):
    """Check multiple sources for page count (AlimulK approach)."""
    for key in ("totalPageCount", "pageCount"):
        v = config.get(key)
        if v and isinstance(v, int):
            return v
    meta = config.get("meta", {})
    for key in ("totalPageCount", "pageCount"):
        v = meta.get(key)
        if v and isinstance(v, int):
            return v
    pages = config.get("fliphtml5_pages", [])
    return len(pages)


def _clean_path(path):
    """Normalize a page path — remove backslashes, deduplicate segments."""
    path = path.replace("\\", "/")
    while "//" in path:
        path = path.replace("//", "/")
    parts = path.split("/")
    cleaned = []
    for p in parts:
        if p == "..":
            if cleaned:
                cleaned.pop()
        elif p and p != ".":
            cleaned.append(p)
    return "/".join(cleaned)


def _resolve_pages(config, base):
    """Extract and resolve page URLs from config (inspired by both repos)."""
    pages_arr = config.get("fliphtml5_pages", [])
    page_count = _get_page_count(config)

    resolved = []
    if pages_arr:
        for p in pages_arr:
            names = p.get("n", [])
            if not names:
                continue
            raw = names[0]
            cleaned = _clean_path(raw)
            if cleaned.startswith("http"):
                resolved.append(cleaned)
            elif cleaned.startswith("files/"):
                resolved.append(f"{base}/{cleaned}")
            else:
                resolved.append(f"{base}/files/large/{cleaned}")

    if not resolved:
        for i in range(1, page_count + 1):
            resolved.append(f"{base}/files/large/{i}.webp")

    return resolved


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
            config = _extract_json(text)
            if not config:
                self._json({"error": "Could not parse config.js"}, 500)
                return
        except Exception as e:
            self._json({"error": f"Failed to fetch config: {e}"}, 500)
            return

        meta = config.get("meta", {})
        pages = _resolve_pages(config, base)
        bc = config.get("bookConfig")
        bc_title = bc.get("BookTitle", "") if isinstance(bc, dict) else ""
        title = (
            bc_title
            or meta.get("title", "")
            or config.get("bookTitle", "")
            or "flipbook"
        )

        self._json({
            "title": title,
            "pageCount": _get_page_count(config),
            "pageWidth": meta.get("pageWidth", 595),
            "pageHeight": meta.get("pageHeight", 842),
            "pages": pages,
        })

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
