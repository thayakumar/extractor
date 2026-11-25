#!/usr/bin/env python3
"""
scribd2pdf — Download a free/public Scribd document as a PDF.

Usage:
    python3 scribd2pdf.py <scribd-url> [output.pdf]

Examples:
    python3 scribd2pdf.py https://www.scribd.com/document/632901643/136-MODUL-T2V-SAINS-KOMPUTER-T5-64-109
    python3 scribd2pdf.py https://www.scribd.com/document/632901643 mybook.pdf

Requires: pip3 install playwright Pillow && playwright install chromium
"""

import os
import re
import sys
import tempfile

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is required:  pip3 install playwright && playwright install chromium")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  pip3 install Pillow")


def extract_page_num(url):
    """Extract page number from URL like .../images/3-c1c7037f2e.jpg?..."""
    m = re.search(r"/images/(\d+)-", url)
    return int(m.group(1)) if m else None


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    url = sys.argv[1]
    if "scribd.com" not in url:
        sys.exit("Not a Scribd URL")

    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        # Intercept page image responses and save them
        saved = {}  # page_num -> image_bytes

        def on_response(resp):
            u = resp.url
            if "html.scribdassets.com" not in u or "/images/" not in u:
                return
            pn = extract_page_num(u)
            if pn is None or pn in saved:
                return
            try:
                saved[pn] = resp.body()
            except Exception:
                pass

        page.on("response", on_response)

        print(f"Loading {url}")
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(2000)

        # Get document info
        info = page.evaluate("""() => {
            const t = document.title || '';
            const m = document.body.innerText.match(/(\\d+)\\s+pages?/i);
            return { title: t.replace(/ \\| PDF$/i, '').trim(), totalPages: m ? parseInt(m[1]) : 0 };
        }""")

        title = info["title"] or "scribd_document"
        total = info["totalPages"]
        print(f'Book: "{title}" — {total} pages')

        # Scroll slowly through the entire document to trigger lazy-loading
        print("Scrolling to load all pages...")
        last_count = 0
        stale_rounds = 0
        while True:
            page.evaluate("window.scrollBy(0, 600)")
            page.wait_for_timeout(500)

            if len(saved) >= total and total > 0:
                break

            if len(saved) == last_count:
                stale_rounds += 1
                if stale_rounds > 50:
                    break
            else:
                stale_rounds = 0
                last_count = len(saved)

            if len(saved) % 10 == 0 and len(saved) != last_count:
                print(f"  {len(saved)}/{total} pages loaded...")

        # Some "pages" in total count are ad interstitials, not real content
        if total > 0 and len(saved) < total:
            print(f"  {len(saved)} content pages captured ({total - len(saved)} were ad slots)")
        else:
            print(f"  {len(saved)} pages captured")
        browser.close()

    if not saved:
        sys.exit("No pages captured — the document may require a subscription.")

    # Assemble PDF
    print("Assembling PDF...")
    ordered_pages = sorted(saved.keys())
    safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:60] or "scribd_document"
    output_pdf = sys.argv[2] if len(sys.argv) > 2 else f"{safe_title}.pdf"

    with tempfile.TemporaryDirectory() as tmpdir:
        images = []
        for pn in ordered_pages:
            img_path = os.path.join(tmpdir, f"{pn:04d}.jpg")
            with open(img_path, "wb") as f:
                f.write(saved[pn])
            images.append(Image.open(img_path).convert("RGB"))

        images[0].save(output_pdf, "PDF", save_all=True, append_images=images[1:])
        for img in images:
            img.close()

    size_mb = os.path.getsize(output_pdf) / (1024 * 1024)
    print(f"Done! {output_pdf} ({size_mb:.1f} MB, {len(ordered_pages)} pages)")


if __name__ == "__main__":
    main()
