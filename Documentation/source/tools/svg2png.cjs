const { chromium } = require("D:/Aproject/SENTINEL AI/frontend/node_modules/@playwright/test");
const fs = require("fs");
(async () => {
  const b = await chromium.launch();
  for (const f of process.argv.slice(2)) {
    const svg = fs.readFileSync(f, "utf8");
    const [, w, h] = svg.match(/width="(\d+)" height="(\d+)"/);
    const p = await b.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: 2 });
    await p.setContent(`<html><body style="margin:0;background:#0B1628">${svg}</body></html>`);
    await p.screenshot({ path: f.replace(".svg", ".png"), fullPage: true });
    await p.pdf({ path: f.replace(".svg", ".pdf"), width: w + "px", height: (+h + 2) + "px", printBackground: true, pageRanges: "1" });
    console.log("rendered", f);
  }
  await b.close();
})();
