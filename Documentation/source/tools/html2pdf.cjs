const { chromium } = require("D:/Aproject/SENTINEL AI/frontend/node_modules/@playwright/test");
const path = require("path");
(async () => {
  const [src, out, mode, label = "SENTINEL VISION · High-Level Design v1.0"] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto(require("url").pathToFileURL(path.resolve(src)).href, { waitUntil: "networkidle" });
  const opts = mode === "slides"
    ? { path: out, width: "13.333in", height: "7.5in", printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 } }
    : { path: out, format: "A4", printBackground: true, displayHeaderFooter: true, headerTemplate: "<span></span>",
        footerTemplate: `<div style="font-size:7pt;color:#8795A8;width:100%;padding:0 16mm;display:flex;justify-content:space-between;font-family:Segoe UI,Arial"><span>${label}</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
        preferCSSPageSize: true };
  await p.pdf(opts);
  await b.close();
  console.log("pdf", out);
})();
