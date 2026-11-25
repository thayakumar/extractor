# Document Extractor

Unified tool to download flipbooks/documents as clean PDFs from AnyFlip and Scribd.

## Project structure

```
anyflip2pdf/              ← Vercel project (GitHub: thayakumar/extractor)
├── api/
│   ├── config.py         ← Serverless: parses AnyFlip URL, fetches config.js, returns page hashes
│   └── proxy.py          ← Serverless: proxies image downloads to bypass CORS
├── public/
│   └── index.html        ← Unified frontend with AnyFlip + Scribd tabs
├── vercel.json           ← Explicit builds array (@vercel/python + @vercel/static)
└── requirements.txt      ← Empty (stdlib only)

../extractor_server.py    ← Local unified server (port 8321), both extractors work
../scribd2pdf.py          ← Standalone CLI for Scribd → PDF (Playwright)
../scribd2pdf_server.py   ← Legacy standalone Scribd server (superseded by extractor_server.py)
```

## Deployment

- **Vercel**: https://extractor-sooty.vercel.app/ — auto-deploys on push to `main`
- **GitHub**: https://github.com/thayakumar/extractor.git
- **Local**: `python3 ../extractor_server.py` on port 8321

## How it works

### AnyFlip
- Page-to-hash mapping lives at `online.anyflip.com/{bookId}/{editionId}/mobile/javascript/config.js` in the `fliphtml5_pages` array
- URLs must be normalized to `online.anyflip.com` (the bare `anyflip.com` domain redirects)
- Images: `https://online.anyflip.com/{bookId}/{editionId}/files/large/{hash}.webp`
- On Vercel: backend proxies config + images, frontend assembles PDF client-side with jsPDF (avoids serverless timeout/size limits)
- Locally: server downloads images in parallel and assembles PDF with Pillow

### Scribd
- Uses aggressive JS-based bot protection ("Client Challenge") — all curl/urllib requests get blocked
- Requires Playwright headless Chrome to bypass protection
- Intercepts network responses for page images (`html.scribdassets.com/.../images/{pageNum}-{hash}.jpg`) during scroll
- Scribd's reported page count includes ad interstitial slots (not real content pages)
- **Local only** — cannot run on Vercel due to Playwright/browser requirement

## Key decisions

- Port 8321 (port 8787 is occupied by caveman-proxy on this machine)
- PDFs saved to `~/Downloads/Personal/extracted_pdfs/`
- 6 concurrent image downloads on Vercel, 8 threads locally
- Single-pass scroll for Scribd (scrolling back causes image unload)
- Stale-rounds detection (50 rounds max) for Scribd scroll completion

## Dependencies

- **Vercel**: stdlib only (no pip packages)
- **Local AnyFlip**: `requests`, `Pillow`
- **Local Scribd**: `playwright`, `Pillow` — plus `playwright install chromium`
