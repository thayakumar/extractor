const chromium = require("@sparticuz/chromium");
const puppeteer = require("puppeteer-core");

const EMBED_URL = "https://www.scribd.com/embeds/{id}/content?view_mode=scroll";

function extractDocId(url) {
  const m = url.match(/scribd\.com\/(?:document|doc|presentation)\/(\d+)/);
  return m ? m[1] : null;
}

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }

  let body = "";
  for await (const chunk of req) body += chunk;
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    res.status(400).json({ error: "Invalid JSON" });
    return;
  }

  const docId = extractDocId(parsed.url || "");
  if (!docId) {
    res.status(400).json({ error: "Invalid Scribd URL" });
    return;
  }

  // SSE setup
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Access-Control-Allow-Origin": "*",
  });

  const send = (event, data) => {
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  };

  let browser;
  try {
    send("status", { status: "launching", message: "Launching browser..." });

    browser = await puppeteer.launch({
      args: [
        ...chromium.args,
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
      ],
      defaultViewport: { width: 1200, height: 1600 },
      executablePath: await chromium.executablePath(),
      headless: chromium.headless,
    });

    const page = await browser.newPage();
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    );

    // Hide automation signals
    await page.evaluateOnNewDocument(() => {
      Object.defineProperty(navigator, "webdriver", { get: () => false });
      delete navigator.__proto__.webdriver;
    });

    send("status", { status: "loading", message: "Loading document..." });

    const embedUrl = EMBED_URL.replace("{id}", docId);
    const response = await page.goto(embedUrl, {
      waitUntil: "domcontentloaded",
      timeout: 60000,
    });

    if (!response || response.status() === 403) {
      send("error", {
        error: "Scribd blocked access (Cloudflare). Try running locally.",
      });
      res.end();
      return;
    }

    try {
      await page.waitForNetworkIdle({ timeout: 15000 });
    } catch {}

    // Check for Cloudflare challenge
    const isBlocked = await page.evaluate(() => {
      const title = document.title.toLowerCase();
      const body = document.body?.innerText?.toLowerCase() || "";
      return (
        title.includes("just a moment") ||
        title.includes("attention required") ||
        body.includes("checking your browser") ||
        body.includes("cloudflare")
      );
    });

    if (isBlocked) {
      send("error", {
        error:
          "Scribd's Cloudflare protection blocked this request. Scribd extraction only works locally for now.",
      });
      res.end();
      return;
    }

    // Dismiss cookie consent
    const consentSelectors = [
      ".osano-cm-accept-all",
      ".osano-cm-accept",
      'button:has-text("Accept All")',
    ];
    for (const sel of consentSelectors) {
      try {
        const btn = await page.$(sel);
        if (btn) {
          await btn.click();
          await page.waitForTimeout(400);
          break;
        }
      } catch {}
    }

    // Try docManager approach first (themrsami method)
    send("status", { status: "loading_pages", message: "Loading pages..." });

    const pageInfo = await page.evaluate(() => {
      const dm =
        window.docManager ||
        (window.Scribd && window.Scribd.Document && window.Scribd.Document.docManager);
      if (dm && dm.pages) {
        return { count: dm.pages.length, hasDocManager: true };
      }
      // Fallback: count .outer_page elements
      const pages = document.querySelectorAll(".outer_page");
      return { count: pages.length, hasDocManager: false };
    });

    if (pageInfo.count === 0) {
      send("error", {
        error: "No pages found. The document may be restricted or require login.",
      });
      res.end();
      return;
    }

    send("info", {
      title: await page.evaluate(
        () =>
          document.querySelector('meta[property="og:title"]')?.content ||
          document.title ||
          "Scribd Document"
      ),
      pageCount: pageInfo.count,
    });

    // Load all pages via docManager or scrolling
    if (pageInfo.hasDocManager) {
      const BATCH = 8;
      for (let start = 0; start < pageInfo.count; start += BATCH) {
        const end = Math.min(start + BATCH, pageInfo.count);
        await page.evaluate(
          async (s, e) => {
            const dm =
              window.docManager ||
              window.Scribd.Document.docManager;
            const promises = [];
            for (let i = s; i < e; i++) {
              const p = dm.pages[i];
              if (p && p.load) promises.push(p.load());
              if (p && p.display) promises.push(p.display());
              if (p && p.turnOnImages) promises.push(p.turnOnImages());
            }
            await Promise.allSettled(promises);
          },
          start,
          end
        );
        send("status", {
          status: "loading_pages",
          message: `Loading pages ${end}/${pageInfo.count}...`,
          done: end,
          total: pageInfo.count,
        });
      }
    } else {
      // Fallback: scroll to lazy-load
      await page.evaluate(async () => {
        const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
        const scroller =
          document.querySelector(".document_scroller") ||
          document.scrollingElement ||
          document.documentElement;
        const step = Math.max(400, Math.floor(scroller.clientHeight * 0.9));
        let pos = 0;
        const total = scroller.scrollHeight;
        while (pos < total) {
          scroller.scrollTop = pos;
          await sleep(300);
          pos += step;
        }
        scroller.scrollTop = scroller.scrollHeight;
        await sleep(600);
        scroller.scrollTop = 0;
        await sleep(400);
      });
    }

    try {
      await page.waitForNetworkIdle({ timeout: 10000 });
    } catch {}

    // Inject CSS to flatten layout and hide chrome
    await page.addStyleTag({
      content: `
        html, body {
          height: auto !important; max-height: none !important;
          overflow: visible !important; margin: 0 !important;
          padding: 0 !important; background: white !important;
        }
        .document_scroller, [class*="document_scroller"], [class*="doc_container"] {
          height: auto !important; max-height: none !important;
          overflow: visible !important; position: static !important;
        }
        .osano-cm-window, .osano-cm-dialog, [class*="osano-cm"],
        .global_header, .embed_header, .embed_footer,
        [class*="toolbar"], [class*="banner"],
        [id*="download"], [class*="download_button"],
        a[href*="download"], button[class*="download" i] {
          display: none !important;
        }
        .outer_page {
          page-break-after: always !important;
          break-after: page !important;
          margin: 0 auto !important;
          box-shadow: none !important;
        }
        .outer_page:last-child {
          page-break-after: auto !important;
          break-after: auto !important;
        }
      `,
    });
    await page.waitForTimeout(500);

    // Screenshot each page individually (avoids blank page issue)
    send("status", {
      status: "capturing",
      message: "Capturing pages...",
      done: 0,
      total: pageInfo.count,
    });

    const pageElements = await page.$$(".outer_page");
    const actualCount = pageElements.length;

    for (let i = 0; i < actualCount; i++) {
      try {
        const el = pageElements[i];

        // Check if page has real content (skip blank pages)
        const hasContent = await el.evaluate((node) => {
          const rect = node.getBoundingClientRect();
          if (rect.width < 10 || rect.height < 10) return false;
          const imgs = node.querySelectorAll("img");
          const text = node.innerText?.trim() || "";
          return imgs.length > 0 || text.length > 10;
        });

        if (!hasContent) continue;

        const screenshot = await el.screenshot({
          type: "jpeg",
          quality: 90,
        });

        const base64 = screenshot.toString("base64");
        send("page", {
          index: i,
          total: actualCount,
          data: base64,
        });

        send("status", {
          status: "capturing",
          message: `Captured page ${i + 1}/${actualCount}`,
          done: i + 1,
          total: actualCount,
        });
      } catch {
        send("page_error", { index: i, message: `Failed to capture page ${i + 1}` });
      }
    }

    send("done", { message: "All pages captured" });
  } catch (err) {
    send("error", { error: err.message || "Unknown error" });
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch {}
    }
    res.end();
  }
};
