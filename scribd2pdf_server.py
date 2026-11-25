#!/usr/bin/env python3
"""
Scribd → PDF  —  local web app.

Usage:
    python3 scribd2pdf_server.py
    Then open http://localhost:8321

Requires: pip3 install playwright Pillow && playwright install chromium
"""

import http.server
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import uuid
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is required:  pip3 install playwright && playwright install chromium")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  pip3 install Pillow")

PORT = 8321
OUTPUT_DIR = Path.home() / "Downloads" / "Personal" / "scribd_pdfs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}


# ── Scribd logic ───────────────────────────────────────────────

def extract_page_num(url):
    m = re.search(r"/images/(\d+)-", url)
    return int(m.group(1)) if m else None


def run_conversion(job_id, url):
    job = jobs[job_id]
    try:
        job["status"] = "launching"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            saved = {}

            def on_response(resp):
                u = resp.url
                if "html.scribdassets.com" not in u or "/images/" not in u:
                    return
                pn = extract_page_num(u)
                if pn is None or pn in saved:
                    return
                try:
                    saved[pn] = resp.body()
                    job["done"] = len(saved)
                except Exception:
                    pass

            page.on("response", on_response)

            job["status"] = "loading"
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(2000)

            info = page.evaluate(r"""() => {
                const t = document.title || '';
                const m = document.body.innerText.match(/(\d+)\s+pages?/i);
                return { title: t.replace(/ \| PDF$/i, '').trim(), totalPages: m ? parseInt(m[1]) : 0 };
            }""")

            job["title"] = info["title"] or "scribd_document"
            job["total"] = info["totalPages"]
            job["status"] = "scrolling"

            last_count = 0
            stale_rounds = 0
            while True:
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(500)

                total = job["total"]
                if total > 0 and len(saved) >= total:
                    break

                if len(saved) == last_count:
                    stale_rounds += 1
                    if stale_rounds > 50:
                        break
                else:
                    stale_rounds = 0
                    last_count = len(saved)

            browser.close()

        if not saved:
            raise ValueError("No pages captured — the document may require a subscription.")

        job["status"] = "assembling"
        job["done"] = len(saved)
        ordered = sorted(saved.keys())

        safe_title = re.sub(r'[^\w\s-]', '', job["title"]).strip()[:60] or "scribd_document"
        pdf_name = f"{safe_title}.pdf"
        pdf_path = OUTPUT_DIR / pdf_name

        with tempfile.TemporaryDirectory() as tmpdir:
            images = []
            for pn in ordered:
                img_path = os.path.join(tmpdir, f"{pn:04d}.jpg")
                with open(img_path, "wb") as f:
                    f.write(saved[pn])
                images.append(Image.open(img_path).convert("RGB"))

            images[0].save(str(pdf_path), "PDF", save_all=True, append_images=images[1:])
            for img in images:
                img.close()

        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        job["status"] = "done"
        job["pdf_path"] = str(pdf_path)
        job["pdf_name"] = pdf_name
        job["size_mb"] = round(size_mb, 1)
        job["pages"] = len(ordered)

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ── HTTP server ────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path.startswith("/api/status/"):
            job_id = self.path.split("/")[-1]
            job = jobs.get(job_id)
            if not job:
                self._json({"error": "not found"}, 404)
            else:
                self._json(job)
        elif self.path.startswith("/api/pdf/"):
            job_id = self.path.split("/")[-1]
            job = jobs.get(job_id)
            if not job or "pdf_path" not in job:
                self.send_error(404)
                return
            with open(job["pdf_path"], "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", len(data))
            self.send_header("Content-Disposition", f'attachment; filename="{job["pdf_name"]}"')
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/preview/"):
            job_id = self.path.split("/")[-1]
            job = jobs.get(job_id)
            if not job or "pdf_path" not in job:
                self.send_error(404)
                return
            with open(job["pdf_path"], "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/convert":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            url = body.get("url", "")
            if "scribd.com" not in url:
                self._json({"error": "Not a Scribd URL"}, 400)
                return
            job_id = uuid.uuid4().hex[:12]
            jobs[job_id] = {"status": "starting", "done": 0, "total": 0, "title": ""}
            t = threading.Thread(target=run_conversion, args=(job_id, url), daemon=True)
            t.start()
            self._json({"job_id": job_id})
        else:
            self.send_error(404)

    def _serve_html(self):
        html = HTML_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(html))
        self.end_headers()
        self.wfile.write(html)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scribd → PDF</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root {
  --bg: #f3f1ee;
  --surface: #ffffff;
  --surface-alt: #eae8e4;
  --fg: #1a1917;
  --fg-dim: #6e6b64;
  --accent: #1a6b4e;
  --accent-hover: #145a40;
  --accent-soft: #e4f3ec;
  --success: #1a6b4e;
  --success-soft: #e4f3ec;
  --border: #d6d3cc;
  --radius: 10px;
  --font: 'DM Sans', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #131210;
    --surface: #1c1b18;
    --surface-alt: #26241f;
    --fg: #e4e1da;
    --fg-dim: #8a877f;
    --accent: #3dbb8a;
    --accent-hover: #53d09f;
    --accent-soft: #172e24;
    --success: #3dbb8a;
    --success-soft: #172e24;
    --border: #36342d;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #131210;
  --surface: #1c1b18;
  --surface-alt: #26241f;
  --fg: #e4e1da;
  --fg-dim: #8a877f;
  --accent: #3dbb8a;
  --accent-hover: #53d09f;
  --accent-soft: #172e24;
  --success: #3dbb8a;
  --success-soft: #172e24;
  --border: #36342d;
  color-scheme: dark;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--fg);
  min-height: 100vh;
  padding: 0 16px;
}

.app {
  max-width: 900px;
  margin: 0 auto;
  padding: 48px 0 80px;
}

.header { margin-bottom: 36px; }
.header h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.4px; margin-bottom: 6px; }
.header h1 span { color: var(--accent); }
.header p { color: var(--fg-dim); font-size: 14px; line-height: 1.5; }

.input-row { display: flex; gap: 10px; margin-bottom: 28px; }
.input-row input {
  flex: 1; min-width: 0;
  padding: 13px 16px;
  font-family: var(--mono); font-size: 13.5px;
  background: var(--surface); color: var(--fg);
  border: 1.5px solid var(--border); border-radius: var(--radius);
  outline: none; transition: border-color .15s;
}
.input-row input:focus { border-color: var(--accent); }
.input-row input::placeholder { color: var(--fg-dim); opacity: .55; }

.btn {
  padding: 13px 26px;
  font-family: var(--font); font-size: 14.5px; font-weight: 600;
  border: none; border-radius: var(--radius);
  cursor: pointer; transition: background .15s, opacity .15s;
  white-space: nowrap;
}
.btn-go { background: var(--accent); color: #fff; }
.btn-go:hover { background: var(--accent-hover); }
.btn-go:disabled { opacity: .45; cursor: not-allowed; }
.btn-dl { background: var(--success); color: #fff; }
.btn-dl:hover { opacity: .9; }

[hidden] { display: none !important; }

.card {
  background: var(--surface);
  border: 1.5px solid var(--border);
  border-radius: var(--radius);
  padding: 22px 24px;
  margin-bottom: 20px;
}
.book-row { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }
.book-icon {
  width: 42px; height: 42px; border-radius: 8px;
  background: var(--accent-soft);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.book-icon svg { width: 20px; height: 20px; color: var(--accent); }
.book-title { font-weight: 600; font-size: 15px; line-height: 1.3; }
.book-pages { font-size: 12.5px; color: var(--fg-dim); margin-top: 2px; }

.prog-label { display: flex; justify-content: space-between; font-size: 12.5px; color: var(--fg-dim); margin-bottom: 7px; }
.prog-track { height: 5px; background: var(--surface-alt); border-radius: 3px; overflow: hidden; }
.prog-fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width .25s ease; width: 0%; }
.prog-fill.ok { background: var(--success); }

.status-line { margin-top: 10px; font-size: 12.5px; color: var(--fg-dim); font-family: var(--mono); }

.done-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 20px; background: var(--success-soft);
  border-radius: var(--radius); margin-bottom: 20px;
}
.done-dot {
  width: 26px; height: 26px; border-radius: 50%;
  background: var(--success);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.done-dot svg { width: 14px; height: 14px; color: #fff; }
.done-info { flex: 1; font-size: 13.5px; font-weight: 500; }

.error-box {
  padding: 14px 20px; background: #fdeae7; color: #8b2518;
  border-radius: var(--radius); font-size: 13.5px; margin-bottom: 20px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .error-box { background: #2e1a17; color: #f0a89e; }
}
:root[data-theme="dark"] .error-box { background: #2e1a17; color: #f0a89e; }

.preview iframe {
  width: 100%; height: 72vh; min-height: 420px;
  border: 1.5px solid var(--border); border-radius: var(--radius);
  background: var(--surface-alt);
}

@media (max-width: 540px) {
  .input-row { flex-direction: column; }
  .btn { width: 100%; }
  .header h1 { font-size: 22px; }
}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>Scribd <span>&rarr;</span> PDF</h1>
    <p>Paste a free Scribd document link. A headless browser loads the pages and assembles a clean PDF.</p>
  </div>

  <div class="input-row">
    <input id="inp" type="url" placeholder="https://www.scribd.com/document/632901643/..." autofocus>
    <button id="go" class="btn btn-go" onclick="run()">Convert</button>
  </div>

  <div id="err" class="error-box" hidden></div>

  <div id="card" class="card" hidden>
    <div class="book-row">
      <div class="book-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
      </div>
      <div>
        <div id="btitle" class="book-title"></div>
        <div id="bpages" class="book-pages"></div>
      </div>
    </div>
    <div class="prog-label">
      <span id="plabel">Starting...</span>
      <span id="pcount"></span>
    </div>
    <div class="prog-track"><div id="pfill" class="prog-fill"></div></div>
    <div id="sline" class="status-line"></div>
  </div>

  <div id="done" class="done-bar" hidden>
    <div class="done-dot">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"/>
      </svg>
    </div>
    <div id="dinfo" class="done-info"></div>
    <button class="btn btn-dl" onclick="downloadPdf()">Download PDF</button>
  </div>

  <div id="pview" class="preview" hidden>
    <iframe id="frame"></iframe>
  </div>
</div>

<script>
let currentJobId = null;
let pollTimer = null;

const $ = id => document.getElementById(id);

const STATUS_MSG = {
  starting: 'Starting...',
  launching: 'Launching browser...',
  loading: 'Loading Scribd page...',
  scrolling: 'Scrolling through pages...',
  assembling: 'Assembling PDF...',
};

function reset() {
  $('err').hidden = true;
  $('card').hidden = true;
  $('done').hidden = true;
  $('pview').hidden = true;
  $('pfill').style.width = '0%';
  $('pfill').classList.remove('ok');
  if (pollTimer) clearInterval(pollTimer);
}

function fail(msg) {
  $('err').textContent = msg;
  $('err').hidden = false;
  $('go').disabled = false;
  $('go').textContent = 'Convert';
}

async function run() {
  const url = $('inp').value.trim();
  if (!url) { $('inp').focus(); return; }

  reset();
  $('go').disabled = true;
  $('go').textContent = 'Converting...';
  $('card').hidden = false;
  $('plabel').textContent = 'Starting...';
  $('sline').textContent = 'Launching headless browser...';

  try {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    currentJobId = data.job_id;
    pollTimer = setInterval(pollStatus, 600);
  } catch (e) {
    fail(e.message);
  }
}

async function pollStatus() {
  if (!currentJobId) return;
  try {
    const res = await fetch(`/api/status/${currentJobId}`);
    const job = await res.json();

    if (job.title) {
      $('btitle').textContent = job.title;
      $('bpages').textContent = job.total ? `${job.total} pages` : '';
    }

    const label = STATUS_MSG[job.status] || job.status;
    $('plabel').textContent = label;

    if (job.status === 'scrolling' && job.total > 0) {
      const pct = Math.round((job.done / job.total) * 100);
      $('pfill').style.width = pct + '%';
      $('pcount').textContent = `${job.done} / ${job.total}`;
      $('sline').textContent = '';
    } else if (job.status === 'assembling') {
      $('pfill').style.width = '100%';
      $('pcount').textContent = '';
      $('sline').textContent = 'Stitching pages together...';
    } else if (job.status === 'done') {
      clearInterval(pollTimer);
      $('pfill').style.width = '100%';
      $('pfill').classList.add('ok');
      $('plabel').textContent = 'Complete';
      $('pcount').textContent = '';
      $('sline').textContent = '';
      $('done').hidden = false;
      $('dinfo').textContent = `${job.pages} pages · ${job.size_mb} MB`;
      $('pview').hidden = false;
      $('frame').src = `/api/preview/${currentJobId}`;
      $('go').disabled = false;
      $('go').textContent = 'Convert';
    } else if (job.status === 'error') {
      clearInterval(pollTimer);
      fail(job.error);
    } else {
      $('sline').textContent = label;
    }
  } catch {}
}

function downloadPdf() {
  if (!currentJobId) return;
  window.open(`/api/pdf/${currentJobId}`, '_blank');
}

$('inp').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
</script>
</body>
</html>
"""


class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    with ReusableServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"\n  Scribd → PDF  running at  {url}\n")
        print(f"  PDFs saved to  {OUTPUT_DIR}\n")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
