"use strict";

const fs = require("fs");
const path = require("path");


async function main() {
  const playwrightRoot = process.env.CODEX_PLAYWRIGHT_ROOT;
  if (!playwrightRoot) throw new Error("CODEX_PLAYWRIGHT_ROOT is required");
  const { chromium } = require(path.join(playwrightRoot, "playwright"));
  const workflow = path.resolve(process.argv[2]);
  const screenshot = path.resolve(process.argv[3]);
  const comfyUrl = process.env.COMFY_URL || "http://127.0.0.1:8190/";
  if (!fs.existsSync(workflow)) throw new Error(`workflow not found: ${workflow}`);

  const browser = await chromium.launch({
    headless: true,
    executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
    args: ["--disable-gpu"],
  });
  const page = await browser.newPage({ viewport: { width: 2560, height: 1440 } });
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  await page.goto(comfyUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(5000);

  const chooserPromise = page.waitForEvent("filechooser", { timeout: 10000 });
  await page.keyboard.press("Control+O");
  const chooser = await chooserPromise;
  await chooser.setFiles(workflow);
  await page.waitForTimeout(6000);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(500);

  const graph = await page.evaluate(() => {
    const candidates = [
      window.app?.graph,
      window.app?.canvas?.graph,
      window.comfyAPI?.app?.graph,
    ];
    const active = candidates.find((value) => value && Array.isArray(value._nodes));
    if (!active) {
      return {
        found: false,
        bodyText: document.body.innerText.slice(0, 4000),
        canvasCount: document.querySelectorAll("canvas").length,
      };
    }
    return {
      found: true,
      nodes: active._nodes.map((node) => ({
        id: node.id,
        type: node.type,
        title: node.title,
        mode: node.mode,
      })),
      groups: (active._groups || []).map((group) => group.title),
    };
  });
  const apiPrompt = await page.evaluate(async () => {
    try {
      const result = await window.app.graphToPrompt();
      const output = result?.output || {};
      return {
        nodeCount: Object.keys(output).length,
        classTypes: [...new Set(Object.values(output).map((node) => node.class_type))].sort(),
      };
    } catch (error) {
      return { error: String(error?.stack || error) };
    }
  });
  await page.screenshot({ path: screenshot, fullPage: true });
  const result = {
    url: page.url(),
    title: await page.title(),
    graph,
    apiPrompt,
    consoleErrors,
    pageErrors,
    screenshot,
  };
  process.stdout.write(JSON.stringify(result, null, 2));
  await browser.close();
}


main().catch((error) => {
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
});
