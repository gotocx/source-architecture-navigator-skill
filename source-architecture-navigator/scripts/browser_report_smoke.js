#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { spawn } = require("node:child_process");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
  const args = { html: "", edge: "" };
  for (let index = 2; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--edge") {
      args.edge = argv[index + 1] || "";
      index += 1;
    } else if (!args.html) {
      args.html = item;
    }
  }
  if (!args.html) {
    throw new Error("Usage: node scripts/browser_report_smoke.js <report.html> [--edge <edge-exe>]");
  }
  return args;
}

function findEdge(explicit) {
  const candidates = [
    explicit,
    process.env.EDGE_PATH,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "microsoft-edge",
    "msedge",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (candidate.includes(path.sep) || candidate.includes(":")) {
      if (fs.existsSync(candidate)) return candidate;
    } else {
      return candidate;
    }
  }
  throw new Error("Microsoft Edge executable was not found. Pass --edge <path>.");
}

async function waitForPage(port, targetUrl) {
  for (let index = 0; index < 60; index += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json`);
      const tabs = await response.json();
      const page = tabs.find((tab) => tab.type === "page" && (tab.url === targetUrl || tab.url.startsWith("file:///")));
      if (page) return page;
    } catch (error) {
      // Browser is still starting.
    }
    await sleep(250);
  }
  throw new Error("Browser debugging endpoint did not expose the report page.");
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    if (typeof WebSocket === "undefined") {
      reject(new Error("This smoke script requires a Node.js runtime with global WebSocket support."));
      return;
    }
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let seq = 0;
    ws.addEventListener("open", () => {
      resolve({
        send(method, params = {}) {
          const id = ++seq;
          ws.send(JSON.stringify({ id, method, params }));
          return new Promise((res, rej) => pending.set(id, { res, rej, method }));
        },
        close() {
          ws.close();
        },
      });
    });
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const item = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) item.rej(new Error(`${item.method}: ${message.error.message}`));
      else item.res(message.result);
    });
    ws.addEventListener("error", reject);
  });
}

async function evaluate(cdp, fn) {
  const result = await cdp.send("Runtime.evaluate", {
    expression: `(${fn.toString()})()`,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    const details = result.exceptionDetails;
    const message = details.exception?.description || details.text || "Page evaluation failed.";
    throw new Error(message);
  }
  return result.result.value;
}

async function waitForReady(cdp) {
  for (let index = 0; index < 40; index += 1) {
    const ready = await evaluate(cdp, () => document.readyState);
    if (ready === "complete" || ready === "interactive") return;
    await sleep(150);
  }
}

function pageSmoke() {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function fireMouse(type, el, detail = 1) {
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: rect.left + Math.min(80, Math.max(12, rect.width / 2)),
      clientY: rect.top + Math.min(28, Math.max(12, rect.height / 2)),
      detail,
    }));
  }
  return (async () => {
    localStorage.clear();
    const summary = {};
    summary.symbolRows = document.querySelectorAll(".symbol-row").length;
    summary.funcCards = document.querySelectorAll(".func-card").length;
    summary.hasInventory = Boolean(document.querySelector("#inventory"));
    summary.hasCopyButton = Boolean(document.querySelector("[data-action=\"copy-symbol-inventory\"]"));

    const target = document.querySelector("#project td") || document.querySelector("#project");
    const target2 = document.querySelector("#coverage .parse-cell") || document.querySelector("#coverage");
    target.scrollIntoView({ block: "center" });
    await sleep(80);
    fireMouse("click", target, 1);
    await sleep(80);
    summary.singleClickEditors = document.querySelectorAll(".inline-note-pad").length;
    fireMouse("dblclick", target, 2);
    await sleep(120);
    summary.doubleClickEditors = document.querySelectorAll(".inline-note-pad").length;
    const input = document.querySelector(".inline-note-pad input");
    if (input) {
      input.value = "browser smoke note";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }

    target2.scrollIntoView({ block: "center" });
    await sleep(80);
    fireMouse("click", target2, 1);
    await sleep(180);
    summary.editorsAfterOutsideClick = document.querySelectorAll(".inline-note-pad").length;
    summary.savedViews = document.querySelectorAll(".inline-note-view").length;
    summary.orderedExport = (document.querySelector("#notesExport")?.value || "").trim();

    const mode = document.querySelector("#noteExportMode");
    if (mode) {
      mode.value = "source";
      mode.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await sleep(80);
    summary.sourceExport = (document.querySelector("#notesExport")?.value || "").trim();

    const search = document.querySelector("#symbolSearch");
    if (search) {
      search.value = "run_pair";
      search.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await sleep(80);
    summary.filteredRows = Array.from(document.querySelectorAll(".symbol-row")).filter((row) => !row.classList.contains("is-hidden")).length;
    summary.filteredCountText = document.querySelector("#symbolCount")?.textContent || "";
    summary.noOldExportLabels = !document.body.innerText.includes("按照顺序") && !document.body.innerText.includes("记录：");
    summary.noVisibleNoteHint = !document.body.innerText.includes("双击正文 + 选中文句");
    return summary;
  })();
}

function noteViewRect() {
  document.querySelector("#project")?.scrollIntoView({ block: "center" });
  window.placeAllLayerItems?.();
  const view = document.querySelector(".inline-note-view");
  if (!view) return null;
  const rect = view.getBoundingClientRect();
  return {
    x: rect.left + Math.min(40, Math.max(12, rect.width / 2)),
    y: rect.top + Math.min(24, Math.max(12, rect.height / 2)),
    left: rect.left,
    top: rect.top,
  };
}

function dragCheck() {
  const view = document.querySelector(".inline-note-view");
  const rect = view ? view.getBoundingClientRect() : null;
  const noteStoreKey = Object.keys(localStorage).find((key) => key.startsWith("source-nav-notes:"));
  const noteStoreValue = noteStoreKey ? localStorage.getItem(noteStoreKey) || "" : "";
  return {
    dragPersisted: noteStoreValue.includes("\"dx\"") && noteStoreValue.includes("\"dy\""),
    noteStoreSample: noteStoreValue.slice(0, 220),
    viewLeft: rect ? Math.round(rect.left) : null,
    viewTop: rect ? Math.round(rect.top) : null,
  };
}

function syntheticDrag() {
  function readDragCheck() {
    const view = document.querySelector(".inline-note-view");
    const rect = view ? view.getBoundingClientRect() : null;
    const noteStoreKey = Object.keys(localStorage).find((key) => key.startsWith("source-nav-notes:"));
    const noteStoreValue = noteStoreKey ? localStorage.getItem(noteStoreKey) || "" : "";
    return {
      dragPersisted: noteStoreValue.includes("\"dx\"") && noteStoreValue.includes("\"dy\""),
      noteStoreSample: noteStoreValue.slice(0, 220),
      viewLeft: rect ? Math.round(rect.left) : null,
      viewTop: rect ? Math.round(rect.top) : null,
    };
  }
  document.querySelector("#project")?.scrollIntoView({ block: "center" });
  window.placeAllLayerItems?.();
  const view = document.querySelector(".inline-note-view");
  if (!view) return readDragCheck();
  const rect = view.getBoundingClientRect();
  function fire(type, x, y) {
    view.dispatchEvent(new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      view: window,
      button: 0,
      buttons: type === "mouseup" ? 0 : 1,
      clientX: x,
      clientY: y,
    }));
  }
  fire("mousedown", rect.left + 24, rect.top + 20);
  fire("mousemove", rect.left + 74, rect.top + 44);
  fire("mouseup", rect.left + 74, rect.top + 44);
  return readDragCheck();
}

function reloadCheck() {
  return {
    savedViewsAfterReload: document.querySelectorAll(".inline-note-view").length,
    exportAfterReload: (document.querySelector("#notesExport")?.value || "").trim(),
  };
}

function assertSmoke(summary, reloadSummary, dragSummary) {
  const failures = [];
  if (summary.symbolRows < 1 || summary.symbolRows > 260) failures.push("symbol inventory count must be 1..260");
  if (!summary.hasInventory || !summary.hasCopyButton) failures.push("inventory or copy button missing");
  if (summary.singleClickEditors !== 0) failures.push("single click created a note editor");
  if (summary.doubleClickEditors !== 1) failures.push("double click did not create one note editor");
  if (summary.editorsAfterOutsideClick !== 0) failures.push("outside click did not close the active editor");
  if (summary.savedViews < 1) failures.push("saved note view did not render");
  if (!summary.orderedExport.startsWith("1. browser smoke note")) failures.push("ordered export format is wrong");
  if (!summary.sourceExport.includes("原文：") || !summary.sourceExport.includes("\n笔记：browser smoke note")) failures.push("source export format is wrong");
  if (!dragSummary.dragPersisted) failures.push("drag offset was not persisted");
  if (summary.filteredRows < 1 || !summary.filteredCountText.includes("/ 260")) failures.push("symbol filter did not operate on full inventory");
  if (!summary.noOldExportLabels) failures.push("old export labels are still visible");
  if (!summary.noVisibleNoteHint) failures.push("visible note hint text is still present");
  if (reloadSummary.savedViewsAfterReload < 1 || !reloadSummary.exportAfterReload.includes("browser smoke note")) failures.push("note did not survive reload");
  if (failures.length) {
    const error = new Error(
      "REPORT_SMOKE_FAILED\n"
      + failures.join("\n")
      + "\nSUMMARY\n"
      + JSON.stringify({ summary, dragSummary, reloadSummary }, null, 2)
    );
    error.failures = failures;
    throw error;
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const reportPath = path.resolve(args.html);
  if (!fs.existsSync(reportPath)) throw new Error(`Report does not exist: ${reportPath}`);
  const edge = findEdge(args.edge);
  const port = 9300 + Math.floor(Math.random() * 500);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "san-browser-"));
  const url = pathToFileURL(reportPath).href;
  const proc = spawn(edge, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--disable-gpu",
    "--no-first-run",
    url,
  ], { stdio: "ignore", windowsHide: true });

  try {
    const page = await waitForPage(port, url);
    const cdp = await connect(page.webSocketDebuggerUrl);
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await waitForReady(cdp);
    const summary = await evaluate(cdp, pageSmoke);
    await evaluate(cdp, noteViewRect);
    const dragSummary = await evaluate(cdp, syntheticDrag);
    await cdp.send("Page.reload", { ignoreCache: true });
    await sleep(1200);
    await waitForReady(cdp);
    const reloadSummary = await evaluate(cdp, reloadCheck);
    assertSmoke(summary, reloadSummary, dragSummary);
    console.log(JSON.stringify({ summary, dragSummary, reloadSummary }, null, 2));
    console.log("REPORT_SMOKE_OK");
    cdp.close();
  } finally {
    proc.kill();
    await sleep(500);
    try {
      fs.rmSync(profile, { recursive: true, force: true });
    } catch (error) {
      // The browser can briefly hold the profile lock after process termination.
    }
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
