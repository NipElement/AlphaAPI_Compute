import puppeteer from "puppeteer-core";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
const require = createRequire(import.meta.url);
import {
  readFileSync,
  mkdirSync,
  writeFileSync,
  readdirSync,
  existsSync,
  appendFileSync,
} from "node:fs";
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const CTX = process.env.KUBE_CONTEXT || "kind-b300-prelab";
if (!CTX.startsWith("kind-")) throw new Error("Quality checks are kind-only");
const out =
  process.env.BROWSER_EVIDENCE ||
  resolve(
    ROOT,
    "evidence/RUN-quality-" + new Date().toISOString().replace(/[-:.]/g, ""),
  );
if (existsSync(out) && readdirSync(out).length)
  throw new Error("Evidence directory must be empty");
for (
  let parent = resolve(out);
  parent !== dirname(parent);
  parent = dirname(parent)
)
  if (existsSync(resolve(parent, "hashes.sha256")))
    throw new Error("Refusing to change sealed evidence");
mkdirSync(out, { recursive: true });
execFileSync("bash", [resolve(ROOT, "scripts/guard.sh"), "check"], {
  stdio: "pipe",
});
const tunnel = spawn(
  "kubectl",
  [
    "--context",
    CTX,
    "-n",
    "platform-system",
    "port-forward",
    "--address=127.0.0.1",
    "svc/platform-gateway",
    "0:8080",
  ],
  { env: { ...process.env, KUBECTL_PORT_FORWARD_WEBSOCKETS: "false" } },
);
tunnel.stderr.on("data", (d) =>
  appendFileSync(resolve(out, "port-forward.log"), d),
);
const base = await new Promise((yes, no) => {
  let output = "";
  const timer = setTimeout(() => {
    tunnel.kill("SIGTERM");
    no(new Error("Port forward timeout"));
  }, 20000);
  timer.unref();
  tunnel.stdout.on("data", (d) => {
    output += d;
    const match = output.match(/127\.0\.0\.1:(\d+)/);
    if (match) {
      clearTimeout(timer);
      yes("http://127.0.0.1:" + match[1]);
    }
  });
  tunnel.on("error", no);
  tunnel.on("exit", () => no(new Error("Port forward exited")));
});
const axe = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const cache = resolve(homedir(), ".cache/puppeteer/chrome");
const executablePath =
  process.env.BROWSER_PATH ||
  (existsSync(cache)
    ? readdirSync(cache)
        .map((v) => resolve(cache, v, "chrome-linux64/chrome"))
        .filter(existsSync)
        .at(-1)
    : undefined);
let b;
const focused = process.env.QUALITY_FOCUS === "popups";
const reports = [],
  errors = [];
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  b = await puppeteer.launch({
    executablePath,
    headless: true,
    args: [
      "--disable-dev-shm-usage",
      ...(process.env.BROWSER_NO_SANDBOX === "1" ? ["--no-sandbox"] : []),
    ],
  });
  writeFileSync(
    resolve(out, "environment.json"),
    JSON.stringify(
      {
        browser: await b.version(),
        context: CTX,
        axeVersion: require("axe-core/package.json").version,
        widths: [1512, 390, 1024, 768, 320],
        themes: ["light", "dark"],
        locales: ["zh", "en"],
      },
      null,
      2,
    ),
  );
  const p = await b.newPage();
  p.on("pageerror", (e) => errors.push(e.message));
  await p.setViewport({ width: 1512, height: 1050 });
  await p.goto(base);
  await p.waitForSelector("input[type=password]");
  let wantedTheme = "light";
  async function theme(wanted) {
    wantedTheme = wanted;
    if (await p.$(".sidebar")) {
      await p.goto(base + "/#/overview");
      await p.waitForSelector(".page-heading");
      await p.keyboard.press("Escape");
      await pause(400);
      await p.waitForFunction(
        () =>
          ![...document.querySelectorAll(".arco-drawer,.arco-modal")].some(
            (e) => e.getClientRects().length,
          ),
      );
      await p.click(".page-heading h1"); // Close a previous account dropdown.
    }
    if ((await p.$eval("html", (e) => e.dataset.theme)) !== wanted)
      await p.click(".theme-toggle");
    await p.waitForFunction(
      (theme) =>
        document.documentElement.dataset.theme === theme &&
        document.body.getAttribute("arco-theme") === theme,
      {},
      wanted,
    );
    await pause(200);
  }
  async function inspect(name, runAxe = true, shot = true) {
    await pause(550);
    await p.waitForFunction(() =>
      ![...document.querySelectorAll(".arco-btn-loading,.arco-spin-loading,.chart-loading,.arco-skeleton")]
        .some((e) => e.getClientRects().length && getComputedStyle(e).visibility !== "hidden" && !e.closest("[aria-hidden=true],[inert]")),
      { timeout: 20000 },
    );
    await p.evaluate(() => document.fonts.ready);
    const actualTheme = await p.$eval("html", (e) => e.dataset.theme);
    const actualBodyTheme = await p.$eval("body", (e) => e.getAttribute("arco-theme"));
    assert.equal(actualBodyTheme, wantedTheme, name + ": component theme must match");
    assert.equal(
      actualTheme,
      wantedTheme,
      name + ": actual theme must match requested theme",
    );
    const layout = await p.evaluate(() => {
      const clipped = [],
        small = [];
      const walk = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
      );
      while (walk.nextNode()) {
        const n = walk.currentNode,
          e = n.parentElement;
        if (
          !e ||
          !n.textContent.trim() ||
          e.closest("[aria-hidden=true], [inert], svg, script, style")
        )
          continue;
        const style = getComputedStyle(e),
          r = e.getBoundingClientRect();
        if (
          !r.width ||
          !r.height ||
          style.visibility === "hidden" ||
          style.display === "none"
        )
          continue;
        if (parseFloat(style.fontSize) < 12)
          small.push({
            text: n.textContent.trim().slice(0, 80),
            font: style.fontSize,
            tag: e.tagName,
            cls: e.className,
          });
        if (
          e.closest(
            ".arco-table,.arco-select-view,.arco-input-wrapper,.arco-select-dropdown,.arco-dropdown,.arco-message-list,.arco-drawer-mask",
          )
        )
          continue;
        const range = document.createRange();
        range.selectNodeContents(n);
        for (const rect of range.getClientRects()) {
          if (rect.right > innerWidth + 2 || rect.left < -2)
            clipped.push({
              text: n.textContent.trim().slice(0, 80),
              cls: e.className,
              left: rect.left,
              right: rect.right,
            });
        }
      }
      const sidebar = document.querySelector(".sidebar");
      const navigationClipping =
        sidebar && !sidebar.inert
          ? [...sidebar.querySelectorAll(".nav-item")]
              .filter((e) => e.scrollWidth > e.clientWidth + 2)
              .map((e) => e.textContent.trim())
          : [];
      const headerActions = [...document.querySelectorAll(".mobile-menu,.command-trigger,.language-button,.theme-toggle,.user-chip")]
        .filter((e) => e.getClientRects().length && !e.closest("[inert]"));
      const headerMisaligned = innerWidth <= 575 && headerActions.length > 1 &&
        Math.max(...headerActions.map((e) => e.getBoundingClientRect().y + e.getBoundingClientRect().height / 2)) -
        Math.min(...headerActions.map((e) => e.getBoundingClientRect().y + e.getBoundingClientRect().height / 2)) > 3;
      const tenantSelect = document.querySelector(".topbar .tenant-select");
      const tenantRowInvalid = innerWidth <= 575 && !!tenantSelect && headerActions.length > 0 &&
        tenantSelect.getBoundingClientRect().top < Math.max(...headerActions.map((e) => e.getBoundingClientRect().bottom));
      return {
        headerMisaligned,
        tenantRowInvalid,
        width: innerWidth,
        overflow: document.documentElement.scrollWidth > innerWidth,
        small,
        clipped,
        navigationClipping,
      };
    });
    let violations = [],
      incomplete = [];
    if (runAxe) {
      await p.evaluate(axe);
      const result = await p.evaluate(
        async () =>
          await axe.run(document, {
            runOnly: {
              type: "tag",
              values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
            },
          }),
      );
      violations = result.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        description: v.description,
        nodes: v.nodes.map((n) => ({
          target: n.target,
          html: n.html,
          summary: n.failureSummary,
        })),
      }));
      incomplete = result.incomplete.map((v) => ({
        id: v.id,
        nodes: v.nodes.length,
        details: v.nodes.map((n) => ({
          target: n.target,
          html: n.html,
          summary: n.failureSummary,
        })),
      }));
    }
    reports.push({
      name,
      actualTheme,
      actualBodyTheme,
      ...layout,
      axeChecked: runAxe,
      violations,
      incomplete,
    });
    writeFileSync(
      out + "/reports.json",
      JSON.stringify({ errors, reports }, null, 2),
    );
    await pause(350);
    if (shot)
      await p.screenshot({ path: out + "/" + name + ".png", fullPage: true });
    process.stdout.write(
      JSON.stringify({
        name,
        overflow: layout.overflow,
        small: layout.small.length,
        clipped: layout.clipped.length,
        violations: violations.map((v) => [v.id, v.nodes.length]),
      }) + "\n",
    );
  }
  for (const mode of ["light", "dark"]) {
    await theme(mode);
    await inspect("login-" + mode);
  }
  await theme("light");
  await p.type("input[type=text]", "admin");
  await p.type(
    "input[type=password]",
    process.env.GW_ADMIN_PASSWORD || "arise-admin",
  );
  await p.click("button[type=submit]");
  await p.waitForSelector(".page-heading");
  const routes = [
    "overview",
    "dev",
    "jobs",
    "services",
    "volumes",
    "images",
    "quota",
    "usage",
    "fleet",
    "monitoring",
    "alerts",
    "audit",
    "users",
  ];
  for (const width of focused ? [] : [1512, 390, 1024, 768, 320]) {
    await p.setViewport({ width, height: width > 1000 ? 1050 : 844 });
    for (const mode of ["light", "dark"]) {
      await theme(mode);
      for (const route of routes) {
        await p.goto(base + "/#/" + route);
        await p.waitForSelector(".page-heading");
        await inspect(
          `${route}-${mode}-${width}`,
          width === 1512,
          width === 1512 || width === 390,
        );
      }
      if (width === 1512 || width === 390)
        for (const route of ["dev", "jobs", "services", "volumes"]) {
          await p.goto(base + "/#/" + route + "?create=1");
          await p.waitForSelector(".arco-drawer", { visible: true });
          await inspect(`${route}-create-${mode}-${width}`, width === 1512);
        }
    }
  }

  // English translations and teleported controls receive their own review.
  await p.setViewport({ width: 1512, height: 1050 });
  await p.click(".language-button");
  for (const mode of ["light", "dark"]) {
    await theme(mode);
    for (const route of focused ? [] : routes) {
      await p.goto(base + "/#/" + route);
      await p.waitForSelector(".page-heading");
      await inspect(`${route}-${mode}-english`, true, false);
    }
    await p.goto(base + "/#/dev?create=1");
    await p.waitForSelector(".arco-drawer", { visible: true });
    await p.click(".arco-drawer .arco-select");
    await inspect(`environment-menu-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.goto(base + "/#/users");
    await p.waitForSelector(".page-heading");
    await pause(400);
    await p.waitForFunction(
      () =>
        ![...document.querySelectorAll(".arco-drawer")].some(
          (e) => e.getClientRects().length,
        ),
    );
    const create = await p.evaluateHandle(() =>
      [...document.querySelectorAll(".page-heading button")].find((e) =>
        e.textContent.includes("New user"),
      ),
    );
    await create.asElement().click();
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-modal")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`new-user-dialog-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await pause(300);
    await p.goto(base + "/#/monitoring");
    await p.waitForSelector(".monitoring-range");
    await p.click(".monitoring-range .arco-select");
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-select-dropdown")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`time-menu-${mode}`, true);
    const custom = await p.evaluateHandle(() =>
      [...document.querySelectorAll(".arco-select-option")].find((e) =>
        e.textContent.includes("Custom time range"),
      ),
    );
    await custom.asElement().click();
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-modal")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`time-dialog-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.click(".command-trigger");
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-modal")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`command-dialog-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.click(".user-chip");
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-dropdown")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`user-menu-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.goto(base + "/#/fleet");
    await p.waitForSelector(".fleet-node");
    await pause(350);
    await p.click(".fleet-node .resource-link");
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-drawer")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await inspect(`node-detail-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.click(".fleet-node footer .arco-btn-secondary");
    await p.waitForFunction(() =>
      [...document.querySelectorAll(".arco-drawer")].some(
        (e) => e.getClientRects().length,
      ),
    );
    await pause(300);
    const direct = await p.evaluateHandle(() =>
      [...document.querySelectorAll(".owner-options .arco-radio-button")].find((e) =>
        e.textContent.includes("DIRECT"),
      ),
    );
    await direct.asElement().click();
    await inspect(`ownership-dialog-${mode}`, true);
    await p.keyboard.press("Escape");
    await pause(350);
    await p.click(".sidebar-footer button");
    await inspect(`collapsed-navigation-${mode}`, true);
    await p.click(".sidebar-footer button");
  }
  assert.equal(reports.length, focused ? 20 : 192, "All declared states must be inspected");
  const failures = reports.filter(
    (r) =>
      r.overflow ||
      r.headerMisaligned ||
      r.tenantRowInvalid ||
      r.small.length ||
      r.clipped.length ||
      r.navigationClipping.length ||
      r.violations.length,
  );
  const result = {
    at: new Date().toISOString(),
    status: failures.length || errors.length ? "FAIL" : "PASS",
    complete: !focused,
    states: reports.length,
    axeStates: reports.filter((r) => r.axeChecked).length,
    pageErrors: errors,
    failures: failures.map((r) => r.name),
    limits:
      "Automated checks and screenshots, not complete WCAG or real-device certification",
  };
  writeFileSync(resolve(out, "results.json"), JSON.stringify(result, null, 2));
  if (result.status !== "PASS") process.exitCode = 1;
} catch (error) {
  errors.push(error.message);
  writeFileSync(
    resolve(out, "results.json"),
    JSON.stringify(
      {
        status: "FAIL",
        complete: false,
        states: reports.length,
        pageErrors: errors,
        error: error.stack,
      },
      null,
      2,
    ),
  );
  process.exitCode = 1;
} finally {
  await b?.close();
  tunnel.kill("SIGTERM");
}
