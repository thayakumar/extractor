#!/usr/bin/env python3
"""
Document Extractor — local web app with AnyFlip + Scribd → PDF.

Usage:
    python3 extractor_server.py
    Then open http://localhost:8321
"""

import http.server
import json
import os
import re
import socketserver
import sys
import tempfile
import threading
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  pip3 install Pillow")

HAS_PLAYWRIGHT = False
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    print("  ⚠ Playwright not installed — Scribd extractor disabled")
    print("    Install with: pip3 install playwright && playwright install chromium\n")

PORT = 8321
OUTPUT_DIR = Path.home() / "Downloads" / "Personal" / "extracted_pdfs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}


# ── AnyFlip ────────────────────────────────────────────────────

def anyflip_convert(job_id, url):
    job = jobs[job_id]
    try:
        url = url.strip().rstrip("/")
        m = re.search(r"https?://[^/]*anyflip\.com/([^/]+/[^/]+)", url)
        if not m:
            raise ValueError("Not a valid AnyFlip URL")
        base = f"https://online.anyflip.com/{m.group(1)}"

        job["status"] = "fetching_config"
        config_url = f"{base}/mobile/javascript/config.js"
        req = urllib.request.Request(config_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")

        cm = re.search(r"var\s+htmlConfig\s*=\s*(\{.+\})\s*;?\s*$", text, re.DOTALL)
        if not cm:
            raise ValueError("Could not parse config.js")
        config = json.loads(cm.group(1))
        pages = config.get("fliphtml5_pages", [])
        if not pages:
            raise ValueError("No pages found")

        meta = config.get("meta", {})
        title = meta.get("title", "flipbook")
        filenames = [p["n"][0] for p in pages if p.get("n")]

        job["title"] = title
        job["total"] = len(filenames)
        job["status"] = "downloading"

        large_base = f"{base}/files/large"

        with tempfile.TemporaryDirectory() as tmpdir:
            results = {}

            def dl(args):
                i, fname = args
                r = urllib.request.Request(f"{large_base}/{fname}", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(r, timeout=60) as resp:
                    data = resp.read()
                path = os.path.join(tmpdir, f"{i:04d}.webp")
                with open(path, "wb") as f:
                    f.write(data)
                return i, path

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(dl, (i, fn)): i for i, fn in enumerate(filenames)}
                for future in as_completed(futures):
                    try:
                        idx, path = future.result()
                        results[idx] = path
                    except Exception:
                        pass
                    job["done"] = len(results)

            if not results:
                raise ValueError("No images downloaded")

            job["status"] = "assembling"
            ordered = [results[i] for i in sorted(results)]
            images = [Image.open(p).convert("RGB") for p in ordered]

            safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:60] or "flipbook"
            pdf_name = f"{safe_title}.pdf"
            pdf_path = OUTPUT_DIR / pdf_name
            images[0].save(str(pdf_path), "PDF", save_all=True, append_images=images[1:])
            for img in images:
                img.close()

        _finish_job(job, pdf_path, pdf_name, len(results))

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ── Scribd ─────────────────────────────────────────────────────

def scribd_convert(job_id, url):
    job = jobs[job_id]
    if not HAS_PLAYWRIGHT:
        job["status"] = "error"
        job["error"] = "Scribd requires Playwright (local only). Install: pip3 install playwright && playwright install chromium"
        return

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
                m = re.search(r"/images/(\d+)-", u)
                if not m:
                    return
                pn = int(m.group(1))
                if pn in saved:
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

        _finish_job(job, pdf_path, pdf_name, len(ordered))

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def _finish_job(job, pdf_path, pdf_name, page_count):
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    job["status"] = "done"
    job["pdf_path"] = str(pdf_path)
    job["pdf_name"] = pdf_name
    job["size_mb"] = round(size_mb, 1)
    job["pages"] = page_count


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
        elif self.path == "/api/capabilities":
            self._json({"scribd": HAS_PLAYWRIGHT})
        elif self.path.startswith("/api/status/"):
            job_id = self.path.split("/")[-1]
            job = jobs.get(job_id)
            self._json(job if job else {"error": "not found"}, 200 if job else 404)
        elif self.path.startswith("/api/pdf/"):
            self._serve_file(self.path.split("/")[-1], download=True)
        elif self.path.startswith("/api/preview/"):
            self._serve_file(self.path.split("/")[-1], download=False)
        else:
            self.send_error(404)

    def _serve_file(self, job_id, download):
        job = jobs.get(job_id)
        if not job or "pdf_path" not in job:
            self.send_error(404)
            return
        with open(job["pdf_path"], "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", len(data))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{job["pdf_name"]}"')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/api/convert":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            url = body.get("url", "")
            source = body.get("source", "")

            if source == "anyflip" and "anyflip.com" not in url:
                self._json({"error": "Not an AnyFlip URL"}, 400)
                return
            if source == "scribd" and "scribd.com" not in url:
                self._json({"error": "Not a Scribd URL"}, 400)
                return

            job_id = uuid.uuid4().hex[:12]
            jobs[job_id] = {"status": "starting", "done": 0, "total": 0, "title": "", "source": source}

            fn = anyflip_convert if source == "anyflip" else scribd_convert
            t = threading.Thread(target=fn, args=(job_id, url), daemon=True)
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
<title>Document Extractor</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root {
  --bg: #f4f2ed;
  --surface: #ffffff;
  --surface-alt: #eae8e3;
  --fg: #1c1b18;
  --fg-dim: #74716a;
  --accent: #2d6a4f;
  --accent-hover: #245840;
  --accent-soft: #dff0e7;
  --scribd-accent: #1a73a7;
  --scribd-soft: #dfeef8;
  --success: #2d6a4f;
  --success-soft: #dff0e7;
  --error-bg: #fdeae7;
  --error-fg: #8b2518;
  --border: #d6d3cc;
  --radius: 10px;
  --font: 'DM Sans', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #141310;
    --surface: #1d1c18;
    --surface-alt: #272520;
    --fg: #e4e1da;
    --fg-dim: #8a877f;
    --accent: #4aad6f;
    --accent-hover: #5ec485;
    --accent-soft: #172e21;
    --scribd-accent: #4eaadd;
    --scribd-soft: #162838;
    --success: #4aad6f;
    --success-soft: #172e21;
    --error-bg: #2e1a17;
    --error-fg: #f0a89e;
    --border: #36342d;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #141310;
  --surface: #1d1c18;
  --surface-alt: #272520;
  --fg: #e4e1da;
  --fg-dim: #8a877f;
  --accent: #4aad6f;
  --accent-hover: #5ec485;
  --accent-soft: #172e21;
  --scribd-accent: #4eaadd;
  --scribd-soft: #162838;
  --success: #4aad6f;
  --success-soft: #172e21;
  --error-bg: #2e1a17;
  --error-fg: #f0a89e;
  --border: #36342d;
  color-scheme: dark;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; }
body { font-family: var(--font); background: var(--bg); color: var(--fg); min-height: 100vh; padding: 0 16px; }
.app { max-width: 880px; margin: 0 auto; padding: 44px 0 80px; }
[hidden] { display: none !important; }

.header { margin-bottom: 32px; }
.header h1 { font-size: 26px; font-weight: 700; letter-spacing: -0.4px; margin-bottom: 4px; }
.header p { color: var(--fg-dim); font-size: 14px; }

/* ── Tabs ── */
.tabs { display: flex; gap: 6px; margin-bottom: 24px; }
.tab {
  padding: 10px 22px;
  font-family: var(--font); font-size: 14px; font-weight: 600;
  border: 1.5px solid var(--border); border-radius: var(--radius);
  background: var(--surface); color: var(--fg-dim);
  cursor: pointer; transition: all .15s;
  display: flex; align-items: center; gap: 8px;
}
.tab:hover { border-color: var(--fg-dim); }
.tab.active-anyflip { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
.tab.active-scribd { border-color: var(--scribd-accent); color: var(--scribd-accent); background: var(--scribd-soft); }
.tab .badge {
  font-size: 10px; font-weight: 600;
  padding: 2px 7px; border-radius: 20px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.badge-local { background: var(--fg-dim); color: var(--bg); }

/* ── Input ── */
.input-row { display: flex; gap: 10px; margin-bottom: 24px; }
.input-row input {
  flex: 1; min-width: 0; padding: 13px 16px;
  font-family: var(--mono); font-size: 13px;
  background: var(--surface); color: var(--fg);
  border: 1.5px solid var(--border); border-radius: var(--radius);
  outline: none; transition: border-color .15s;
}
.input-row input:focus { border-color: var(--accent); }
.input-row input::placeholder { color: var(--fg-dim); opacity: .5; }
.btn {
  padding: 13px 24px; font-family: var(--font); font-size: 14px; font-weight: 600;
  border: none; border-radius: var(--radius); cursor: pointer;
  transition: background .15s, opacity .15s; white-space: nowrap;
}
.btn-go { background: var(--accent); color: #fff; }
.btn-go:hover { background: var(--accent-hover); }
.btn-go:disabled { opacity: .4; cursor: not-allowed; }
.btn-go.scribd { background: var(--scribd-accent); }
.btn-dl { background: var(--success); color: #fff; }
.btn-dl:hover { opacity: .9; }

/* ── Card ── */
.card {
  background: var(--surface); border: 1.5px solid var(--border);
  border-radius: var(--radius); padding: 22px 24px; margin-bottom: 20px;
}
.book-row { display: flex; align-items: center; gap: 14px; margin-bottom: 18px; }
.book-icon {
  width: 42px; height: 42px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.book-icon.anyflip { background: var(--accent-soft); }
.book-icon.anyflip svg { color: var(--accent); }
.book-icon.scribd { background: var(--scribd-soft); }
.book-icon.scribd svg { color: var(--scribd-accent); }
.book-icon svg { width: 20px; height: 20px; }
.book-title { font-weight: 600; font-size: 15px; line-height: 1.3; }
.book-pages { font-size: 12.5px; color: var(--fg-dim); margin-top: 2px; }

.prog-label { display: flex; justify-content: space-between; font-size: 12.5px; color: var(--fg-dim); margin-bottom: 7px; }
.prog-track { height: 5px; background: var(--surface-alt); border-radius: 3px; overflow: hidden; }
.prog-fill { height: 100%; border-radius: 3px; transition: width .25s ease; width: 0%; background: var(--accent); }
.prog-fill.scribd { background: var(--scribd-accent); }
.prog-fill.ok { background: var(--success); }
.status-line { margin-top: 10px; font-size: 12.5px; color: var(--fg-dim); font-family: var(--mono); }

.done-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 14px 20px; background: var(--success-soft);
  border-radius: var(--radius); margin-bottom: 20px;
}
.done-dot {
  width: 26px; height: 26px; border-radius: 50%; background: var(--success);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.done-dot svg { width: 14px; height: 14px; color: #fff; }
.done-info { flex: 1; font-size: 13.5px; font-weight: 500; }

.error-box { padding: 14px 20px; background: var(--error-bg); color: var(--error-fg); border-radius: var(--radius); font-size: 13.5px; margin-bottom: 20px; }

.preview iframe {
  width: 100%; height: 72vh; min-height: 400px;
  border: 1.5px solid var(--border); border-radius: var(--radius); background: var(--surface-alt);
}

.disabled-note {
  padding: 14px 20px; background: var(--surface-alt); color: var(--fg-dim);
  border-radius: var(--radius); font-size: 13.5px; margin-bottom: 20px;
  border: 1.5px dashed var(--border);
}

@media (max-width: 540px) {
  .input-row { flex-direction: column; }
  .btn { width: 100%; }
  .header h1 { font-size: 22px; }
  .tabs { flex-direction: column; }
}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>Document Extractor</h1>
    <p>Download flipbooks and documents as clean PDFs.</p>
  </div>

  <div class="tabs">
    <button class="tab active-anyflip" onclick="switchTab('anyflip')" id="tab-anyflip">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
      AnyFlip
    </button>
    <button class="tab" onclick="switchTab('scribd')" id="tab-scribd">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
      Scribd
      <span class="badge badge-local">Local</span>
    </button>
  </div>

  <div id="scribd-disabled" class="disabled-note" hidden>
    Scribd extraction requires a headless browser and only works when running locally. On Vercel, use AnyFlip.
  </div>

  <div class="input-row">
    <input id="inp" type="url" autofocus>
    <button id="go" class="btn btn-go" onclick="run()">Convert</button>
  </div>

  <div id="err" class="error-box" hidden></div>

  <div id="card" class="card" hidden>
    <div class="book-row">
      <div class="book-icon" id="bicon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
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
    <div class="done-dot"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></div>
    <div id="dinfo" class="done-info"></div>
    <button class="btn btn-dl" onclick="downloadPdf()">Download PDF</button>
  </div>

  <div id="pview" class="preview" hidden>
    <iframe id="frame"></iframe>
  </div>
</div>

<script>
let activeTab = 'anyflip';
let currentJobId = null;
let pollTimer = null;
let scribdEnabled = false;

const $ = id => document.getElementById(id);

const PLACEHOLDERS = {
  anyflip: 'https://anyflip.com/tnvex/hdnr/',
  scribd: 'https://www.scribd.com/document/632901643/...',
};

const STATUS_MSG = {
  starting: 'Starting...',
  fetching_config: 'Fetching book config...',
  downloading: 'Downloading pages...',
  launching: 'Launching browser...',
  loading: 'Loading page...',
  scrolling: 'Scrolling through pages...',
  assembling: 'Assembling PDF...',
};

function switchTab(tab) {
  activeTab = tab;
  $('tab-anyflip').className = tab === 'anyflip' ? 'tab active-anyflip' : 'tab';
  $('tab-scribd').className = tab === 'scribd' ? 'tab active-scribd' : 'tab';
  $('inp').placeholder = PLACEHOLDERS[tab];
  $('go').className = 'btn btn-go' + (tab === 'scribd' ? ' scribd' : '');

  if (tab === 'scribd' && !scribdEnabled) {
    $('scribd-disabled').hidden = false;
    $('go').disabled = true;
  } else {
    $('scribd-disabled').hidden = true;
    $('go').disabled = false;
  }

  reset();
}

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
  $('sline').textContent = '';
  $('bicon').className = 'book-icon ' + activeTab;
  $('pfill').className = 'prog-fill' + (activeTab === 'scribd' ? ' scribd' : '');

  try {
    const res = await fetch('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, source: activeTab })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    currentJobId = data.job_id;
    pollTimer = setInterval(pollStatus, 500);
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

    if ((job.status === 'downloading' || job.status === 'scrolling') && job.total > 0) {
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
    }
  } catch {}
}

function downloadPdf() {
  if (!currentJobId) return;
  window.open(`/api/pdf/${currentJobId}`, '_blank');
}

$('inp').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });

// Check capabilities on load
fetch('/api/capabilities').then(r => r.json()).then(c => {
  scribdEnabled = c.scribd;
  if (activeTab === 'scribd') switchTab('scribd');
}).catch(() => {});

switchTab('anyflip');
</script>
</body>
</html>
"""


class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    with ReusableServer(("", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"\n  Document Extractor  running at  {url}")
        print(f"  PDFs saved to  {OUTPUT_DIR}")
        if HAS_PLAYWRIGHT:
            print(f"  Scribd extraction: enabled")
        print()
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
