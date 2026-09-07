// Real Chromium + deployed kind backends. Only failure/production-shape cases
// intercept responses, explicitly named below. Never accepts a production URL.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import {
  mkdtempSync,
  readFileSync,
  mkdirSync,
  readdirSync,
  existsSync,
  rmSync,
  writeFileSync,
  appendFileSync,
} from "node:fs";
import { tmpdir, homedir } from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import puppeteer from "puppeteer-core";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const CTX = process.env.KUBE_CONTEXT || "kind-b300-prelab";
if (!CTX.startsWith("kind-"))
  throw new Error("Browser acceptance is kind-only");
const OUT =
  process.env.BROWSER_EVIDENCE ||
  resolve(
    ROOT,
    "evidence/RUN-browser-" + new Date().toISOString().replace(/[-:.]/g, ""),
    "browser",
  );
if (existsSync(OUT) && readdirSync(OUT).length)
  throw new Error(
    "Evidence output must be empty; choose a new BROWSER_EVIDENCE directory",
  );
for (
  let parent = resolve(OUT);
  parent !== dirname(parent);
  parent = dirname(parent)
) {
  if (existsSync(resolve(parent, "hashes.sha256")))
    throw new Error("Refusing to write into sealed evidence");
}
mkdirSync(OUT, { recursive: true });
const prefix = "browser-" + randomBytes(4).toString("hex");
const password = randomBytes(20).toString("base64url");
const changedPassword = randomBytes(20).toString("base64url");
const scratch = mkdtempSync(resolve(tmpdir(), "alphaapi-browser-"));
const k = ["--context", CTX];
let browser, tunnel, base, admin, customer;
let registeredTests = 0;
const errors = [],
  results = [];
const labels = {
  overview: "Overview",
  dev: "Dev Machines",
  jobs: "Compute Jobs",
  services: "Online Services",
  volumes: "Volumes",
  images: "Environments",
  quota: "Resource Quotas",
  usage: "Usage",
  fleet: "Fleet",
  monitoring: "Monitoring",
  alerts: "Alerts",
  audit: "Audit",
  users: "Users",
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function text(page, selector = "body") {
  return page.$eval(selector, (e) => e.innerText);
}
async function present(page, selector) {
  return page.evaluate(
    (s) =>
      [...document.querySelectorAll(s)].some((e) => e.getClientRects().length),
    selector,
  );
}
async function fill(page, selector, value) {
  await page.waitForFunction(
    (s) =>
      [...document.querySelectorAll(s)].some((e) => e.getClientRects().length),
    {},
    selector,
  );
  await page.evaluate(
    (s, v) => {
      const e = [...document.querySelectorAll(s)].find(
        (e) => e.getClientRects().length,
      );
      e.value = v;
      e.dispatchEvent(new Event("input", { bubbles: true }));
      e.dispatchEvent(new Event("change", { bubbles: true }));
    },
    selector,
    String(value),
  );
}
async function fillNumber(page, selector, value) {
  const input = await page.waitForSelector(selector, { visible: true });
  await input.click();
  await page.keyboard.down("Control");
  await page.keyboard.press("a");
  await page.keyboard.up("Control");
  await page.keyboard.type(String(value));
  await page.keyboard.press("Tab");
  await wait(100);
}
async function clickText(page, selector, label) {
  await wait(250);
  await page.waitForFunction(
    (s, t) =>
      [...document.querySelectorAll(s)].some(
        (e) =>
          e.getClientRects().length &&
          !e.disabled &&
          e.textContent.replace(/\s/g, "") === t.replace(/\s/g, ""),
      ),
    {},
    selector,
    label,
  );
  const element = await page.evaluateHandle(
    (s, t) =>
      [...document.querySelectorAll(s)].find(
        (e) =>
          e.getClientRects().length &&
          !e.disabled &&
          e.textContent.replace(/\s/g, "") === t.replace(/\s/g, ""),
      ),
    selector,
    label,
  );
  await element.asElement().scrollIntoView();
  // Wait until a transient toast/animation no longer covers the click target.
  await page.waitForFunction(
    (e) => {
      const r = e.getBoundingClientRect();
      const hit = document.elementFromPoint(
        r.x + r.width / 2,
        r.y + r.height / 2,
      );
      return hit && e.contains(hit);
    },
    {},
    element,
  );
  await element.asElement().asLocator().click();
  await element.dispose();
  await wait(250);
}
async function see(page, needle, selector = "body") {
  await page.waitForFunction(
    (s, t) =>
      [...document.querySelectorAll(s)].some(
        (e) => e.getClientRects().length && e.innerText.includes(t),
      ),
    { timeout: 20000 },
    selector,
    needle,
  );
}
async function route(page, path) {
  await page.goto(base + "/#/" + path, { waitUntil: "domcontentloaded" });
  await page.waitForFunction((p) => location.hash === "#/" + p, {}, path);
  if (labels[path])
    await page.waitForFunction(
      (title) => document.title === `${title} · ARISE Compute`,
      {},
      labels[path],
    );
  await wait(350);
}
async function settled(page) {
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll(".arco-spin-loading")].some(
        (e) => e.getClientRects().length,
      ),
    { timeout: 25000 },
  );
  await wait(200);
}
async function openCreate(page) {
  await page.locator('[data-action="create-resource"]').click();
  await page.waitForSelector('.arco-drawer input[aria-label="Resource name"]', {
    visible: true,
  });
  await wait(250);
}
async function resourceAction(page, name, action) {
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll(".arco-drawer")].some(
        (e) => e.getClientRects().length,
      ),
  );
  await wait(150);
  await page.locator(`table button[aria-label="Actions: ${name}"]`).click();
  await clickText(page, ".arco-dropdown-option", action);
}
async function deleteResource(page, name) {
  await resourceAction(page, name, "Delete");
  await clickText(page, ".arco-modal button", "Delete resource");
  await page.waitForFunction(
    (name) =>
      ![...document.querySelectorAll("table .resource-link")].some(
        (e) => e.textContent === name,
      ),
    {},
    name,
  );
}
async function closeDrawer(page) {
  await page.locator(".arco-drawer-close-btn").click();
  await page.waitForFunction(
    () =>
      ![...document.querySelectorAll(".arco-drawer")].some(
        (e) => e.getClientRects().length,
      ),
  );
  await wait(250);
}
async function request(page, path, method = "GET", body) {
  return page.evaluate(
    async ({ path, method, body }) => {
      const r = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      return { status: r.status, body: await r.json() };
    },
    { path, method, body },
  );
}
async function newPage() {
  const c = await browser.createBrowserContext();
  const p = await c.newPage();
  await p.setViewport({ width: 1440, height: 1000 });
  p.on("pageerror", (e) => errors.push(e.message));
  p.setDefaultTimeout(10000);
  await p.evaluateOnNewDocument(() => localStorage.setItem("arise-lang", "en"));
  await p.goto(base, { waitUntil: "domcontentloaded" });
  await p.waitForSelector("input[type=password],.topbar");
  return p;
}
async function login(page, name, pw) {
  await route(page, "login");
  await fill(page, "input[type=text]", name);
  await fill(page, "input[type=password]", pw);
  await page.click("button[type=submit]");
  await page.waitForFunction(() => location.hash === "#/overview");
  await wait(400);
}
async function intercept(page, fn, action) {
  await page.setRequestInterception(true);
  const handler = async (req) => {
    try {
      if (!(await fn(req))) await req.continue();
    } catch (e) {
      errors.push(e.message);
      if (!req.isInterceptResolutionHandled()) await req.abort();
    }
  };
  page.on("request", handler);
  try {
    return await action();
  } finally {
    page.off("request", handler);
    await page.setRequestInterception(false);
  }
}
async function failResponse(
  req,
  body = "Acceptance: backend temporarily unavailable",
  status = 503,
) {
  await req.respond({
    status,
    contentType: "application/json",
    headers: { "Cache-Control": "no-store" },
    body: JSON.stringify({ error: body }),
  });
  return true;
}
function check(name, fn) {
  registeredTests++;
  return test(name, { timeout: 90000 }, async () => {
    try {
      await fn();
      results.push({ name, status: "PASS" });
    } catch (e) {
      results.push({ name, status: "FAIL", error: e.message });
      for (const [role, page] of [
        ["admin", admin],
        ["customer", customer],
      ])
        try {
          await page?.screenshot({
            path: resolve(
              OUT,
              "failure-" + results.length + "-" + role + ".png",
            ),
            fullPage: true,
          });
          writeFileSync(
            resolve(OUT, "failure-" + results.length + "-" + role + ".txt"),
            await text(page),
          );
        } catch {}
      throw e;
    }
  });
}

before(async () => {
  execFileSync("bash", [resolve(ROOT, "scripts/guard.sh"), "check"], {
    stdio: "pipe",
  });
  tunnel = spawn(
    "kubectl",
    [
      ...k,
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
    appendFileSync(resolve(OUT, "port-forward.log"), d),
  );
  let output = "";
  const port = await new Promise((yes, no) => {
    tunnel.stdout.on("data", (d) => {
      output += d;
      const m = output.match(/127\.0\.0\.1:(\d+)/);
      if (m) yes(Number(m[1]));
    });
    tunnel.on("exit", () => no(new Error("port-forward exited")));
    setTimeout(() => no(new Error("port-forward timeout")), 20000).unref();
  });
  base = "http://127.0.0.1:" + port;
  const cache = resolve(homedir(), ".cache/puppeteer/chrome");
  const cached = existsSync(cache)
    ? readdirSync(cache)
        .map((v) => resolve(cache, v, "chrome-linux64/chrome"))
        .filter(existsSync)
        .at(-1)
    : undefined;
  const executablePath = process.env.BROWSER_PATH || cached;
  if (!executablePath)
    throw new Error(
      "Set BROWSER_PATH to an installed Chrome/Chromium executable",
    );
  browser = await puppeteer.launch({
    executablePath,
    headless: true,
    userDataDir: resolve(scratch, "profile"),
    args: [
      "--disable-dev-shm-usage",
      "--disable-background-networking",
      ...(process.env.BROWSER_NO_SANDBOX === "1" ? ["--no-sandbox"] : []),
    ],
  });
  execFileSync("ssh-keygen", [
    "-q",
    "-t",
    "ed25519",
    "-N",
    "",
    "-f",
    resolve(scratch, "machine-key"),
  ]);
  admin = await newPage();
  await login(admin, "admin", process.env.GW_ADMIN_PASSWORD || "arise-admin");
  const created = await request(admin, "/auth/users", "POST", {
    username: prefix,
    password,
    role: "user",
    tenant: "tenant-arise",
    display: prefix,
  });
  assert.equal(created.status, 201);
  customer = await newPage();
  await login(customer, prefix, password);
  writeFileSync(
    resolve(OUT, "environment.json"),
    JSON.stringify(
      {
        browser: await browser.version(),
        context: CTX,
        viewport: [1440, 1000],
        prefix,
        actualBackends: true,
      },
      null,
      2,
    ),
  );
});
after(async () => {
  const cleanupErrors = [];
  async function remove(path) {
    try {
      const response = await request(admin, path, "DELETE");
      if (![200, 404].includes(response.status))
        cleanupErrors.push({ path, status: response.status });
    } catch (e) {
      cleanupErrors.push({ path, error: e.message });
    }
  }
  try {
    if (admin) {
      for (const ns of ["tenant-arise", "tenant-direct"])
        for (const [kind, name] of [
          ["devmachines", prefix],
          ["services", prefix],
          ["jobs", prefix],
          ["volumes", prefix],
          ["volumes", prefix + "-data"],
        ])
          await remove(`/papi/${kind}/${name}?ns=${ns}`);
      for (const name of [prefix, prefix + "-ui"])
        await remove("/auth/users/" + name);
    }
  } finally {
    await browser?.close();
    tunnel?.kill();
    rmSync(scratch, { recursive: true, force: true });
    try {
      execFileSync("bash", [resolve(ROOT, "scripts/guard.sh"), "check"], {
        stdio: "pipe",
      });
    } catch {
      cleanupErrors.push({ error: "shared-host guard failed" });
    }
    const complete = results.length === registeredTests;
    const status =
      complete &&
      results.every((r) => r.status === "PASS") &&
      !errors.length &&
      !cleanupErrors.length
        ? "PASS"
        : "FAIL";
    writeFileSync(
      resolve(OUT, "results.json"),
      JSON.stringify(
        {
          at: new Date().toISOString(),
          status,
          complete,
          registeredTests,
          results,
          pageErrors: errors,
          cleanupErrors,
        },
        null,
        2,
      ),
    );
    assert.deepEqual(cleanupErrors, [], "fixture cleanup must succeed");
  }
});

check(
  "unauthenticated deep links redirect and bad login remains on login",
  async () => {
    const p = await newPage();
    await p.goto(base + "/#/dev");
    await p.waitForFunction(() => location.hash === "#/login");
    assert.equal(
      await p.$eval("html", (e) => e.lang),
      "en",
      "persisted locale applies on first load",
    );
    assert.equal(await p.title(), "Sign in · ARISE Compute");
    await fill(p, "input[type=text]", prefix);
    await fill(p, "input[type=password]", "wrong-password");
    await p.click("button[type=submit]");
    await see(p, "bad credentials");
    assert.equal(new URL(p.url()).hash, "#/login");
    await p.browserContext().close();
  },
);
check(
  "all 13 admin pages render against the actual gateway without runtime errors",
  async () => {
    for (const path of [
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
    ]) {
      await clickText(admin, ".sidebar .nav-item", labels[path]);
      await see(admin, labels[path], ".topbar .title");
      await settled(admin);
      assert.equal(await admin.title(), `${labels[path]} · ARISE Compute`);
      assert.ok((await text(admin, ".content")).trim().length > 15, path);
    }
    assert.deepEqual(errors, []);
    await admin.screenshot({ path: resolve(OUT, "admin-users.png") });
  },
);
check(
  "all 8 customer pages render; admin routes and APIs are denied",
  async () => {
    for (const path of [
      "overview",
      "dev",
      "jobs",
      "services",
      "volumes",
      "images",
      "quota",
      "usage",
    ]) {
      await clickText(customer, ".sidebar .nav-item", labels[path]);
      await see(customer, labels[path], ".topbar .title");
      await settled(customer);
    }
    assert.equal(
      await customer.$$eval(".sidebar .nav-item", (es) => es.length),
      8,
    );
    await customer.goto(base + "/#/users");
    await customer.waitForFunction(() => location.hash === "#/overview");
    assert.equal((await request(customer, "/auth/users")).status, 403);
    assert.equal(
      (await request(customer, "/papi/overview?ns=tenant-direct")).status,
      403,
    );
  },
);
check(
  "admin account creation handles validation, success and deletion through the UI",
  async () => {
    await route(admin, "users");
    await clickText(admin, "button", "New user");
    await fill(admin, ".arco-modal input[placeholder=alice]", prefix + "-ui");
    await fill(admin, ".arco-modal input[type=password]", "short");
    await clickText(admin, ".arco-modal button", "Create");
    await see(admin, "12–1024");
    assert.equal(await present(admin, ".arco-modal"), true);
    await fill(admin, ".arco-modal input[type=password]", password);
    await clickText(admin, ".arco-modal button", "Create");
    await see(admin, prefix + "-ui", "table");
    await admin.waitForFunction(
      () =>
        ![...document.querySelectorAll(".arco-modal")].some(
          (e) => e.getClientRects().length,
        ),
    );
    const row = await admin.evaluateHandle(
      (name) =>
        [...document.querySelectorAll("tr")].find((e) =>
          e.innerText.includes(name),
        ),
      prefix + "-ui",
    );
    await row
      .asElement()
      .$("button")
      .then((b) => b.click());
    await clickText(admin, ".arco-modal button", "Delete");
    await admin.waitForFunction(
      (name) =>
        ![...document.querySelectorAll("tr")].some((e) =>
          e.innerText.includes(name),
        ),
      {},
      prefix + "-ui",
    );
  },
);
check(
  "customer creates a development machine through the UI; errors preserve form input",
  async () => {
    await route(customer, "dev");
    assert.equal(
      await present(customer, ".arco-drawer"),
      false,
      "resource list is the default view",
    );
    await openCreate(customer);
    await fill(customer, 'input[aria-label="Resource name"]', "INVALID_NAME");
    await customer.locator('[data-action="submit-resource"]').click();
    await see(customer, "Use 1–58", ".arco-drawer");
    assert.equal(
      await customer.$eval('input[aria-label="Resource name"]', (e) => e.value),
      "INVALID_NAME",
    );
    await fill(customer, 'input[aria-label="Resource name"]', prefix);
    await fill(
      customer,
      'textarea[aria-label="SSH public key"]',
      readFileSync(resolve(scratch, "machine-key.pub"), "utf8").trim(),
    );
    await customer.locator('[data-action="submit-resource"]').click();
    await see(customer, prefix, "table");
    await customer.waitForFunction(
      () =>
        ![...document.querySelectorAll(".arco-drawer")].some(
          (e) => e.getClientRects().length,
        ),
    );
  },
);
check(
  "drawer backend error is visible, retry works and errors do not escape into page runtime",
  async () => {
    await route(customer, "dev");
    await see(customer, prefix, "table");
    await intercept(
      customer,
      async (req) =>
        new URL(req.url()).pathname === "/papi/instances"
          ? failResponse(req)
          : false,
      async () => {
        await clickText(customer, "table .resource-link", prefix);
        await see(
          customer,
          "Acceptance: backend temporarily unavailable",
          ".arco-drawer",
        );
      },
    );
    await clickText(customer, ".arco-drawer button", "Refresh");
    await see(customer, prefix, ".arco-drawer .instance-list");
    await customer.locator(".arco-drawer-close-btn").click();
    await wait(300);
  },
);
check(
  "SSH key rotation failure keeps the dialog and input available",
  async () => {
    await route(customer, "dev");
    await see(customer, prefix, "table");
    await resourceAction(customer, prefix, "Replace key");
    await fill(customer, ".arco-modal textarea", "ssh-ed25519 AAAA");
    await clickText(customer, ".arco-modal button", "Ok");
    await see(customer, "invalid");
    assert.equal(await present(customer, ".arco-modal"), true);
    assert.equal(
      await customer.$eval(".arco-modal textarea", (e) => e.value),
      "ssh-ed25519 AAAA",
    );
    await clickText(customer, ".arco-modal button", "Cancel");
  },
);
check(
  "customer creates, inspects and deletes an online service through the UI",
  async () => {
    await route(customer, "services");
    await openCreate(customer);
    await fill(customer, 'input[aria-label="Resource name"]', prefix);
    await customer.locator('[data-action="submit-resource"]').click();
    await see(customer, prefix, "table");
    await clickText(customer, "table .resource-link", prefix);
    await clickText(customer, ".arco-tabs-tab", "Events");
    await settled(customer);
    await closeDrawer(customer);
    await deleteResource(customer, prefix);
  },
);
check(
  "customer creates and deletes a standalone volume through the UI",
  async () => {
    await route(customer, "volumes");
    await openCreate(customer);
    await fill(customer, 'input[aria-label="Resource name"]', prefix);
    await customer.locator('[data-action="submit-resource"]').click();
    await see(customer, prefix, "table");
    await deleteResource(customer, prefix);
  },
);
check(
  "customer submits and deletes a GPU job through UI using simulated GPUs",
  async () => {
    await route(customer, "jobs");
    await openCreate(customer);
    await fill(customer, 'input[aria-label="Resource name"]', prefix);
    await fillNumber(customer, 'input[aria-label="Instances"]', 2);
    await fill(
      customer,
      ".arco-drawer textarea",
      'import os, time; print(os.environ["HOSTNAME"], flush=True); time.sleep(120)',
    );
    await customer.locator('[data-action="submit-resource"]').click();
    await see(customer, prefix, "table");
    await customer.waitForFunction(
      () =>
        ![...document.querySelectorAll(".arco-drawer")].some(
          (e) => e.getClientRects().length,
        ),
    );
    let pods = [];
    for (let i = 0; i < 40; i++) {
      const r = await request(
        customer,
        `/papi/instances?ns=tenant-arise&workload=${prefix}`,
      );
      pods = r.body.instances ?? [];
      if (pods.length === 2 && pods.every((p) => p.phase === "Running")) break;
      await wait(1000);
    }
    assert.equal(
      pods.filter((p) => p.phase === "Running").length,
      2,
      "both replicas scheduled",
    );
    await clickText(customer, "table .resource-link", prefix);
    await clickText(customer, ".arco-tabs-tab", "Logs");
    await see(customer, prefix, ".arco-drawer .code-block pre");
    const first = (await text(customer, ".logs-toolbar .arco-select")).trim();
    const other = pods.find((p) => p.name !== first)?.name;
    assert.ok(other, JSON.stringify(pods));
    await customer.click(".logs-toolbar .arco-select");
    await clickText(customer, ".arco-select-option", other);
    await see(customer, other, ".arco-drawer .code-block pre");
    await customer.screenshot({ path: resolve(OUT, "job-replica-logs.png") });
    await closeDrawer(customer);
    await deleteResource(customer, prefix);
  },
);
check(
  "switching tenant destroys old resource forms and open details",
  async () => {
    await route(admin, "dev");
    await see(admin, prefix, "table");
    await openCreate(admin);
    await fill(admin, 'input[aria-label="Resource name"]', "old-tenant-draft");
    await closeDrawer(admin);
    await clickText(admin, "table .resource-link", prefix);
    await closeDrawer(admin);
    await admin.locator(".topbar .tenant-select").click();
    await clickText(admin, ".arco-select-option", "tenant-direct");
    await admin.waitForFunction(() =>
      document
        .querySelector(".topbar .arco-select")
        ?.innerText.includes("tenant-direct"),
    );
    await settled(admin);
    assert.equal(await present(admin, ".arco-drawer"), false);
    assert.ok(!(await text(admin, ".resource-panel")).includes(prefix));
    await openCreate(admin);
    assert.equal(
      await admin.$eval('input[aria-label="Resource name"]', (e) => e.value),
      "",
    );
    await closeDrawer(admin);
    await admin.click(".topbar .arco-select");
    await clickText(admin, ".arco-select-option", "tenant-arise");
  },
);
check(
  "production-shaped quota data renders real GPU, millicores, GiB and totals",
  async () => {
    const quota = {
      "requests.nvidia.com/gpu": { used: "2", hard: "8" },
      "requests.cpu": { used: "1500m", hard: "8" },
      "requests.memory": { used: "1536Mi", hard: "2Gi" },
    };
    await intercept(
      customer,
      async (req) => {
        if (new URL(req.url()).pathname != "/papi/overview") return false;
        await req.respond({
          status: 200,
          contentType: "application/json",
          headers: { "Cache-Control": "no-store" },
          body: JSON.stringify({
            namespace: "tenant-arise",
            queue: "arise-internal",
            quota,
            jobs: [],
            devmachines: [],
            services: [],
            volumes: [],
          }),
        });
        return true;
      },
      async () => {
        await route(customer, "overview");
        await settled(customer);
        const values = await customer.$$eval(".arco-statistic", (es) =>
          es.slice(0, 3).map((e) => e.innerText.replace(/\s/g, "")),
        );
        assert.ok(values[0].includes("1.50/8"), values);
        assert.ok(values[1].includes("1.50/2"), values);
        assert.equal(
          await customer.$eval(".capacity-number", (e) =>
            e.innerText.replace(/\s/g, ""),
          ),
          "6GPU",
        );
        assert.ok(
          (
            await customer.$eval(".capacity-viz svg", (e) =>
              e.getAttribute("aria-label"),
            )
          ).includes("25.0%"),
        );
      },
    );
  },
);
check("mixed Kubernetes quantity units render correct quota bars", async () => {
  const quota = {
    "requests.memory": { used: "1024Gi", hard: "2Ti" },
    "requests.cpu": { used: "500m", hard: "2" },
  };
  await intercept(
    customer,
    async (req) => {
      if (new URL(req.url()).pathname != "/papi/overview") return false;
      await req.respond({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({
          namespace: "tenant-arise",
          queue: "arise-internal",
          quota,
          jobs: [],
          devmachines: [],
          services: [],
          volumes: [],
        }),
      });
      return true;
    },
    async () => {
      await route(customer, "quota");
      await see(customer, "1,024", ".quota-grid");
      const widths = await customer.$$eval(".quota-meter-fill", (es) =>
        es.map((e) => e.style.width),
      );
      assert.deepEqual(widths, ["25%", "50%"]);
      assert.ok(!(await text(customer)).includes("fixed per-pod overhead"));
    },
  );
});
check(
  "monitoring requests hardware metrics, and unavailable monitoring/audit is shown as an error",
  async () => {
    const queries = [];
    await intercept(
      admin,
      async (req) => {
        const u = new URL(req.url());
        if (u.pathname.startsWith("/prom/")) {
          queries.push(u.searchParams.get("query"));
          return failResponse(req);
        }
        if (u.pathname === "/oapi/fleet") return failResponse(req);
        return false;
      },
      async () => {
        await route(admin, "monitoring");
        await see(
          admin,
          "Acceptance: backend temporarily unavailable",
          ".content",
        );
        assert.ok(queries.some((q) => q?.includes("nvidia_com_gpu")));
        await route(admin, "audit");
        await see(
          admin,
          "Acceptance: backend temporarily unavailable",
          ".content",
        );
        assert.ok(!(await text(admin, ".content")).includes("No events"));
      },
    );
  },
);
check(
  "expired user-management session redirects to login; invalid JSON produces a visible error",
  async () => {
    const p = await newPage();
    await login(p, "admin", process.env.GW_ADMIN_PASSWORD || "arise-admin");
    await intercept(
      p,
      async (req) =>
        new URL(req.url()).pathname === "/auth/users"
          ? failResponse(req, "unauthenticated", 401)
          : false,
      async () => {
        await p.goto(base + "/#/users", { waitUntil: "domcontentloaded" });
        await p.waitForFunction(() => location.hash === "#/login");
      },
    );
    await p.browserContext().close();
    await intercept(
      customer,
      async (req) => {
        if (new URL(req.url()).pathname != "/papi/overview") return false;
        await req.respond({
          status: 200,
          contentType: "text/html",
          headers: { "Cache-Control": "no-store" },
          body: "<html>proxy error</html>",
        });
        return true;
      },
      async () => {
        await route(customer, "usage");
        await route(customer, "volumes");
        await see(customer, "invalid service response");
      },
    );
  },
);
check(
  "password change validates in place, invalidates the old login and accepts the new password",
  async () => {
    await route(customer, "overview");
    await customer.click(".user-chip");
    await clickText(customer, ".arco-dropdown-option", "Change password");
    await fill(
      customer,
      ".arco-modal input[type=password]:first-of-type",
      password,
    );
    const inputs = await customer.$$(".arco-modal input[type=password]");
    await inputs[1].type("short");
    await clickText(customer, ".arco-modal button", "Ok");
    await see(customer, "12");
    assert.equal(await present(customer, ".arco-modal"), true);
    await customer.$$eval(
      ".arco-modal input[type=password]",
      (es, values) =>
        es.forEach((e, i) => {
          e.value = values[i];
          e.dispatchEvent(new Event("input", { bubbles: true }));
        }),
      ["wrong-current-password", changedPassword],
    );
    await clickText(customer, ".arco-modal button", "Ok");
    await see(customer, "current password does not match");
    assert.equal(await present(customer, ".arco-modal"), true);
    await customer.$$eval(
      ".arco-modal input[type=password]",
      (es, values) =>
        es.forEach((e, i) => {
          e.value = values[i];
          e.dispatchEvent(new Event("input", { bubbles: true }));
        }),
      [password, changedPassword],
    );
    await clickText(customer, ".arco-modal button", "Ok");
    await customer.waitForFunction(() => location.hash === "#/login");
    await fill(customer, "input[type=text]", prefix);
    await fill(customer, "input[type=password]", password);
    await customer.click("button[type=submit]");
    await see(customer, "bad credentials");
    await fill(customer, "input[type=password]", changedPassword);
    await customer.click("button[type=submit]");
    await customer.waitForFunction(() => location.hash === "#/overview");
  },
);
check(
  "logout failure does not pretend logout succeeded; real logout blocks protected pages",
  async () => {
    await intercept(
      customer,
      async (req) =>
        new URL(req.url()).pathname === "/auth/logout"
          ? failResponse(req)
          : false,
      async () => {
        await customer.click(".user-chip");
        await clickText(customer, ".arco-dropdown-option", "Sign out");
        await see(customer, "Acceptance: backend temporarily unavailable");
        assert.notEqual(new URL(customer.url()).hash, "#/login");
      },
    );
    await customer.click(".user-chip");
    await clickText(customer, ".arco-dropdown-option", "Sign out");
    await customer.waitForFunction(() => location.hash === "#/login");
    await customer.goto(base + "/#/dev");
    await customer.waitForFunction(() => location.hash === "#/login");
  },
);
check(
  "English/Chinese switch, dark theme and narrow login viewport render without runtime errors",
  async () => {
    await route(admin, "overview");
    await admin.locator(".language-button").click();
    await see(admin, "概览", ".topbar .title");
    assert.equal(await admin.$eval("html", (e) => e.lang), "zh-CN");
    assert.equal(await admin.title(), "概览 · ARISE Compute");
    await admin.locator(".language-button").click();
    await admin.locator(".topbar .theme-toggle").click();
    assert.equal(await admin.$eval("html", (e) => e.dataset.theme), "dark");
    await admin.screenshot({ path: resolve(OUT, "admin-dark.png") });
    await customer.setViewport({ width: 390, height: 844 });
    await customer.screenshot({
      path: resolve(OUT, "customer-login-mobile.png"),
    });
    assert.equal(
      await customer.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    assert.deepEqual(errors, []);
  },
);

check(
  "authentication outage on reload is visible and recovers the existing session",
  async () => {
    const p = await newPage();
    await login(p, "admin", process.env.GW_ADMIN_PASSWORD || "arise-admin");
    await intercept(
      p,
      async (req) =>
        new URL(req.url()).pathname === "/auth/me" ? failResponse(req) : false,
      async () => {
        await p.reload({ waitUntil: "domcontentloaded" });
        await see(p, "Service temporarily unavailable");
        assert.equal(await present(p, "input[type=password]"), false);
      },
    );
    await clickText(p, "button", "Refresh");
    await p.waitForSelector(".topbar");
    assert.ok((await text(p, ".topbar")).includes("Admin"));
    await p.browserContext().close();
  },
);
check(
  "a missing lazy page asset has a visible refresh recovery without a reload loop",
  async () => {
    const p = await newPage();
    let blocked = 0;
    await intercept(
      p,
      async (req) => {
        if (!new URL(req.url()).pathname.match(/AppLayout-.*\.js$/))
          return false;
        blocked++;
        await req.respond({
          status: 404,
          contentType: "text/plain",
          headers: { "Cache-Control": "no-store" },
          body: "Simulated removed deployment asset",
        });
        return true;
      },
      async () => {
        await fill(p, "input[type=text]", "admin");
        await fill(
          p,
          "input[type=password]",
          process.env.GW_ADMIN_PASSWORD || "arise-admin",
        );
        await p.click("button[type=submit]");
        await see(p, "Service temporarily unavailable");
        assert.ok(blocked > 0);
        await wait(500);
        assert.equal(blocked, 1);
      },
    );
    await clickText(p, "button", "Refresh");
    await p.waitForSelector(".topbar");
    assert.equal(new URL(p.url()).hash, "#/overview");
    await p.browserContext().close();
  },
);

check(
  "all data views show persistent backend errors and catalog creation can be retried",
  async () => {
    await route(admin, "users");
    await intercept(
      admin,
      async (req) =>
        /^\/(papi|oapi|prom|am)\//.test(new URL(req.url()).pathname) ||
        new URL(req.url()).pathname === "/auth/users"
          ? failResponse(req)
          : false,
      async () => {
        for (const path of Object.keys(labels)) {
          await route(admin, path);
          await see(
            admin,
            "Acceptance: backend temporarily unavailable",
            ".content",
          );
        }
      },
    );
    await route(admin, "usage");
    await route(admin, "images");
    await see(admin, "python-3.12", ".runtime-grid");
    await intercept(
      admin,
      async (req) =>
        new URL(req.url()).pathname === "/papi/flavors"
          ? failResponse(req)
          : false,
      async () => {
        await route(admin, "dev");
        await openCreate(admin);
        await see(
          admin,
          "Acceptance: backend temporarily unavailable",
          ".arco-drawer",
        );
        assert.equal(
          await admin.$eval(
            '[data-action="submit-resource"]',
            (e) => e.disabled,
          ),
          true,
        );
      },
    );
    await clickText(admin, ".arco-drawer .arco-alert button", "Refresh");
    await admin.waitForFunction(
      () =>
        !document.querySelector('[data-action="submit-resource"]')?.disabled,
    );
    assert.equal(await present(admin, ".arco-drawer .arco-alert-error"), false);
    await closeDrawer(admin);
  },
);
check(
  "customer workbench navigation and creation forms fit a narrow viewport",
  async () => {
    await login(customer, prefix, changedPassword);
    await customer.setViewport({ width: 390, height: 844 });
    for (const path of ["overview", "dev", "jobs", "services", "volumes"]) {
      await route(customer, path);
      await settled(customer);
      if (path !== "overview") await openCreate(customer);
      const clipped = await customer.$$eval(
        ".arco-form input, .arco-form textarea, .arco-form button",
        (es) =>
          es
            .filter((e) => {
              const r = e.getBoundingClientRect();
              return (
                r.width > 0 &&
                r.height > 0 &&
                (r.x < 0 || r.right > innerWidth + 1)
              );
            })
            .map(
              (e) =>
                e.getAttribute("placeholder") ||
                e.getAttribute("type") ||
                e.tagName,
            ),
      );
      assert.deepEqual(
        clipped,
        [],
        path + ": form controls must not be clipped",
      );
      assert.equal(
        await customer.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
        path,
      );
      if (path !== "overview") await closeDrawer(customer);
    }
    await customer.screenshot({
      path: resolve(OUT, "customer-workbench-mobile.png"),
    });
    assert.deepEqual(errors, []);
  },
);

check(
  "command palette supports keyboard search and only offers authorized pages",
  async () => {
    await customer.setViewport({ width: 1440, height: 1000 });
    await route(customer, "overview");
    await customer.keyboard.down("Control");
    await customer.keyboard.press("k");
    await customer.keyboard.up("Control");
    await see(customer, "Search pages", ".arco-modal");
    await fill(customer, ".arco-modal input", "Fleet");
    await see(customer, "No matching pages", ".arco-modal");
    await fill(customer, ".arco-modal input", "Usage");
    await customer.click(".arco-modal input");
    await customer.keyboard.press("Enter");
    await customer.waitForFunction(() => location.hash === "#/usage");
    await customer.waitForFunction(
      () =>
        ![...document.querySelectorAll(".arco-modal")].some(
          (e) => e.getClientRects().length,
        ),
    );
    assert.equal(await present(customer, ".arco-modal"), false);
  },
);
check(
  "overview and runtime shortcuts open creation with real configuration previews",
  async () => {
    await route(customer, "overview");
    await customer.locator(".launcher.blue").click();
    await see(customer, "Create dev machine", ".arco-drawer");
    await see(customer, "python-3.12", ".configuration");
    await clickText(customer, ".compute-modes button", "GPU compute");
    const flavor = await customer.evaluateHandle(() =>
      [...document.querySelectorAll(".fcard")].find(
        (b) => b.querySelector("strong")?.textContent === "2 × GPU",
      ),
    );
    await flavor.asElement().click();
    await flavor.dispose();
    const config = (await text(customer, ".config-totals")).replace(/\s/g, "");
    assert.ok(
      config.includes("2GPU") &&
        config.includes("62vCPU") &&
        config.includes("496GiBRAM"),
      config,
    );
    await customer.screenshot({
      path: resolve(OUT, "customer-create-gpu.png"),
    });
    await closeDrawer(customer);
    assert.equal(new URL(customer.url()).hash, "#/dev");
    await route(customer, "images");
    await clickText(customer, ".runtime-actions button", "Use runtime");
    await customer.waitForSelector(
      '.arco-drawer input[aria-label="Resource name"]',
      { visible: true },
    );
    assert.ok(new URL(customer.url()).hash.includes("image=python-3.12"));
    await see(customer, "python-3.12", ".configuration");
    await closeDrawer(customer);
  },
);
check(
  "resource search, filter reset and SSH access commands work without exposing private endpoints as public links",
  async () => {
    await route(customer, "dev");
    await see(customer, prefix, "table");
    await fill(customer, ".toolbar-search input", "no-such-resource");
    await see(customer, "No matching resources", ".resource-panel");
    await clickText(customer, "button", "Clear filters");
    await see(customer, prefix, "table");
    await customer.screenshot({
      path: resolve(OUT, "customer-resource-list.png"),
    });
    const row = await customer.evaluateHandle(
      (name) =>
        [...document.querySelectorAll("tr")].find(
          (r) => r.querySelector(".resource-link")?.textContent === name,
        ),
      prefix,
    );
    const connect = await row.asElement().$("button.arco-btn-secondary");
    await connect.click();
    await row.dispose();
    await see(customer, "Connection guide", ".arco-drawer");
    assert.equal(
      await present(customer, ".arco-drawer .code-block"),
      false,
      "no invented gateway hostname",
    );
    await fill(
      customer,
      'input[aria-label="SSH gateway hostname"]',
      "bad;command",
    );
    await see(customer, "Enter a valid hostname", ".arco-drawer");
    assert.equal(await present(customer, ".arco-drawer .code-block"), false);
    await fill(
      customer,
      'input[aria-label="SSH gateway hostname"]',
      "ssh.example.com",
    );
    const command = await text(customer, ".code-block pre");
    assert.ok(command.includes("StrictHostKeyChecking=yes"));
    assert.ok(command.includes(`HostKeyAlias=${prefix}.tenant-arise`));
    assert.ok(command.includes(`proxy ${prefix} --host ssh.example.com`));
    await customer.bringToFront();
    await clickText(customer, ".code-block button", "Copy");
    await see(customer, "Copied", ".code-block");
    await customer
      .browserContext()
      .setDownloadBehavior({ policy: "allow", downloadPath: scratch });
    await clickText(customer, ".arco-drawer button", "Download client");
    for (
      let i = 0;
      i < 40 && !existsSync(resolve(scratch, "arise-client.py"));
      i++
    )
      await wait(100);
    assert.equal(
      readFileSync(resolve(scratch, "arise-client.py"), "utf8"),
      readFileSync(resolve(ROOT, "services/ssh-bastion/client.py"), "utf8"),
    );
    await customer.screenshot({ path: resolve(OUT, "customer-ssh-guide.png") });
    await closeDrawer(customer);
  },
);
check(
  "usage CSV downloads the displayed allocation records with explicit units",
  async () => {
    await route(customer, "usage");
    await settled(customer);
    await fill(customer, ".toolbar-search input", prefix);
    await see(customer, prefix, "table");
    await customer
      .browserContext()
      .setDownloadBehavior({ policy: "allow", downloadPath: scratch });
    await clickText(customer, "button", "Export CSV");
    let file;
    for (let i = 0; i < 40; i++) {
      file = readdirSync(scratch).find((n) => n.endsWith(".csv"));
      if (file) break;
      await wait(100);
    }
    assert.ok(file, "CSV download completed");
    const csv = readFileSync(resolve(scratch, file), "utf8");
    assert.ok(
      csv.includes('"opened_at","closed_at","hours","gpu","storage_gib"'),
    );
    assert.ok(csv.includes(prefix));
    await customer.screenshot({
      path: resolve(OUT, "customer-usage-filtered.png"),
    });
  },
);
check(
  "mobile navigation opens all authorized links and closes after selection",
  async () => {
    await customer.setViewport({ width: 390, height: 844 });
    await route(customer, "overview");
    await customer.locator(".mobile-menu").click();
    await customer.waitForSelector(".sidebar.mobile-open");
    await customer.waitForFunction(
      () =>
        document.querySelector(".sidebar.mobile-open")?.getBoundingClientRect()
          .left >= -0.5,
    );
    const positions = await customer.$$eval(".sidebar .nav-item", (es) =>
      es.map((e) => ({
        x: e.getBoundingClientRect().x,
        right: e.getBoundingClientRect().right,
      })),
    );
    assert.ok(
      positions.every((r) => r.x >= 0 && r.right <= 390),
      JSON.stringify(positions),
    );
    await clickText(customer, ".sidebar .nav-item", "Dev Machines");
    await customer.waitForFunction(() => location.hash === "#/dev");
    assert.equal(await present(customer, ".sidebar-backdrop"), false);
    await openCreate(customer);
    await customer.screenshot({
      path: resolve(OUT, "customer-create-mobile.png"),
    });
    await closeDrawer(customer);
    await customer.setViewport({ width: 1440, height: 1000 });
  },
);
check(
  "fleet ownership controls expose checked gates before allowing a transition",
  async () => {
    await route(admin, "fleet");
    await settled(admin);
    await clickText(admin, ".fleet-node .resource-link", "dgx01");
    await see(admin, "Hardware spec", ".arco-drawer");
    await clickText(admin, ".arco-tabs-tab", "Gates");
    await see(admin, "ARISE", ".arco-drawer");
    await closeDrawer(admin);
    const button = await admin.$(".fleet-node footer .arco-btn-secondary");
    await button.click();
    await see(admin, "Change ownership", ".arco-drawer");
    await see(admin, "Transition gate evaluation", ".arco-drawer");
    assert.equal(
      await admin.$eval(
        ".arco-drawer-footer .arco-btn-primary",
        (e) => e.disabled,
      ),
      true,
      "approver is required",
    );
    await admin.screenshot({
      path: resolve(OUT, "fleet-transition-review.png"),
    });
    await closeDrawer(admin);
  },
);

check(
  "creation API failures remain visible beside the submit action and preserve the complete draft",
  async () => {
    await route(customer, "services");
    await openCreate(customer);
    await fill(
      customer,
      'input[aria-label="Resource name"]',
      prefix + "-retry",
    );
    await fillNumber(customer, 'input[aria-label="Instances"]', 3);
    const draft = await customer.$eval(".arco-drawer textarea", (e) => e.value);
    const config = (await text(customer, ".config-totals")).replace(/\s/g, "");
    assert.ok(config.includes("12vCPU") && config.includes("48GiBRAM"), config);
    let submits = 0;
    await intercept(
      customer,
      async (req) => {
        if (
          new URL(req.url()).pathname === "/papi/services" &&
          req.method() === "POST"
        ) {
          submits++;
          return failResponse(req);
        }
        return false;
      },
      async () => {
        await customer.locator('[data-action="submit-resource"]').click();
        await see(
          customer,
          "Acceptance: backend temporarily unavailable",
          ".arco-drawer-footer",
        );
        assert.equal(
          await customer.$eval(
            'input[aria-label="Resource name"]',
            (e) => e.value,
          ),
          prefix + "-retry",
        );
        assert.equal(
          await customer.$eval(".arco-drawer textarea", (e) => e.value),
          draft,
        );
        assert.equal(
          await customer.$eval('input[aria-label="Instances"]', (e) => e.value),
          "3",
        );
        const r = await customer.$eval(
          ".arco-drawer-footer .arco-alert",
          (e) => ({
            y: e.getBoundingClientRect().top,
            bottom: e.getBoundingClientRect().bottom,
          }),
        );
        assert.ok(r.y >= 0 && r.bottom <= 1000, JSON.stringify(r));
      },
    );
    assert.equal(submits, 1);
    await closeDrawer(customer);
  },
);
check(
  "monitoring time range and keyboard chart inspection use real metric requests",
  async () => {
    await route(admin, "monitoring");
    await settled(admin);
    const requests = [];
    const listener = (req) => {
      const url = new URL(req.url());
      if (url.pathname.endsWith("/query_range"))
        requests.push({
          start: Number(url.searchParams.get("start")),
          end: Number(url.searchParams.get("end")),
        });
    };
    admin.on("request", listener);
    try {
      await admin.click(".monitoring-range .arco-select");
      await clickText(admin, ".arco-select-option", "Last 7 days");
      await settled(admin);
      assert.ok(
        requests.some((q) => q.end - q.start === 7 * 86400),
        JSON.stringify(requests),
      );
      const chart = await admin.$(".chart svg");
      assert.ok(chart, "capacity chart rendered with actual metric samples");
      await chart.evaluate((e) => e.focus());
      await admin.keyboard.press("Home");
      const label = await chart.evaluate((e) => e.getAttribute("aria-label"));
      assert.ok(label.includes("GPU") && !label.includes("NaN"), label);
      await admin.screenshot({ path: resolve(OUT, "monitoring-7d-dark.png") });
    } finally {
      admin.off("request", listener);
    }
  },
);

check(
  "theme button toggles directly, survives reload and keeps the shell geometry stable",
  async () => {
    await admin.setViewport({ width: 1440, height: 1000 });
    await route(admin, "overview");
    await settled(admin);
    const measure = () =>
      admin.evaluate(() =>
        [".sidebar", ".main-shell", ".topbar", ".page-heading"].map((s) => {
          const r = document.querySelector(s).getBoundingClientRect();
          return [r.x, r.width, r.height];
        }),
      );
    const before = await measure();
    const original = await admin.$eval("html", (e) => e.dataset.theme);
    await admin.click(".theme-toggle");
    assert.equal(
      await admin.$eval("html", (e) => e.dataset.theme),
      original === "dark" ? "light" : "dark",
    );
    assert.equal(
      await present(admin, ".arco-dropdown"),
      false,
      "theme toggle does not open a menu",
    );
    assert.deepEqual(await measure(), before);
    await admin.reload();
    await settled(admin);
    assert.equal(
      await admin.$eval("html", (e) => e.dataset.theme),
      original === "dark" ? "light" : "dark",
    );
    await admin.click(".theme-toggle");
    assert.deepEqual(await measure(), before);
  },
);

check(
  "custom monitoring validates absolute times, queries the selected historical range and exports samples",
  async () => {
    await route(admin, "monitoring");
    await settled(admin);
    await admin.click(".monitoring-range .arco-select");
    await clickText(admin, ".arco-select-option", "Custom time range");
    const now = Math.floor(Date.now() / 60000) * 60;
    const local = (seconds) => {
      const d = new Date(seconds * 1000),
        pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    await fill(admin, "#monitor-start", local(now));
    await fill(admin, "#monitor-end", local(now - 3600));
    await clickText(admin, ".arco-modal button", "Apply range");
    await see(admin, "The end must be after the start.", ".arco-modal");
    await fill(admin, "#monitor-start", local(now - 86400));
    await fill(admin, "#monitor-end", local(now + 86400));
    await clickText(admin, ".arco-modal button", "Apply range");
    await see(admin, "The end cannot be in the future.", ".arco-modal");
    await fill(admin, "#monitor-start", local(now - 91 * 86400));
    await fill(admin, "#monitor-end", local(now));
    await clickText(admin, ".arco-modal button", "Apply range");
    await see(admin, "Query up to 90 days at once.", ".arco-modal");
    const requests = [],
      listener = (req) => {
        const u = new URL(req.url());
        if (u.pathname.endsWith("/query_range"))
          requests.push(Object.fromEntries(u.searchParams));
      };
    admin.on("request", listener);
    try {
      await fill(admin, "#monitor-start", local(now - 2 * 86400));
      await clickText(admin, ".arco-modal button", "Apply range");
      await settled(admin);
      assert.equal(await present(admin, ".arco-modal"), false);
      assert.ok(
        requests.some(
          (q) => Number(q.start) === now - 2 * 86400 && Number(q.end) === now,
        ),
      );
      assert.ok(
        requests.every(
          (q) => (Number(q.end) - Number(q.start)) / Number(q.step) <= 600,
        ),
      );
      await see(admin, "Fixed historical window", ".monitoring-status");
      await admin
        .browserContext()
        .setDownloadBehavior({ policy: "allow", downloadPath: scratch });
      await clickText(admin, ".page-heading button", "Export metrics CSV");
      for (
        let i = 0;
        i < 30 && !existsSync(resolve(scratch, "compute-metrics.csv"));
        i++
      )
        await wait(100);
      const csv = readFileSync(resolve(scratch, "compute-metrics.csv"), "utf8");
      assert.ok(csv.startsWith("timestamp_utc,metric,value\r\n"));
      assert.ok(csv.includes(",allocatable_gpu,"));
      assert.ok(!csv.includes("NaN"));
      await admin.screenshot({ path: resolve(OUT, "monitoring-custom.png") });
    } finally {
      admin.off("request", listener);
    }
  },
);

check(
  "monitoring draws gaps and retains the requested dates for partial history",
  async () => {
    await intercept(
      admin,
      async (req) => {
        const u = new URL(req.url());
        if (!u.pathname.endsWith("/query_range")) return false;
        const start = Number(u.searchParams.get("start")),
          end = Number(u.searchParams.get("end")),
          step = Number(u.searchParams.get("step"));
        await req.respond({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "success",
            data: {
              result: [
                {
                  metric: {},
                  values: [
                    [start + (end - start) * 0.6, "32"],
                    [start + (end - start) * 0.6 + step, "31"],
                    [end - step, "30"],
                    [end, "32"],
                  ],
                },
              ],
            },
          }),
        });
        return true;
      },
      async () => {
        await route(admin, "monitoring");
        await settled(admin);
        await admin.click(".monitoring-range .arco-select");
        await clickText(admin, ".arco-select-option", "Last 30 days");
        await settled(admin);
        await see(admin, "Only part of this window has historical data");
        const chart = await admin.$(".chart svg");
        const path = await chart.$eval('path[fill="none"]', (e) =>
          e.getAttribute("d"),
        );
        assert.equal(
          (path.match(/M/g) || []).length,
          2,
          "missing samples must not be connected with a fabricated line",
        );
        assert.ok(
          Number(path.match(/^M([\d.]+)/)[1]) > 100,
          "partial data starts inside the full time axis",
        );
        await chart.evaluate((e) => e.focus());
        await admin.keyboard.press("Home");
        assert.ok(
          (await chart.evaluate((e) => e.getAttribute("aria-label"))).includes(
            "GPU",
          ),
        );
        await admin.screenshot({
          path: resolve(OUT, "monitoring-partial-history.png"),
        });
      },
    );
  },
);

check(
  "monitoring can pause automatic refresh without disabling manual refresh",
  async () => {
    await route(admin, "monitoring");
    await settled(admin);
    await clickText(admin, "button", "Pause refresh");
    const requests = [],
      listener = (req) => {
        if (new URL(req.url()).pathname.endsWith("/query_range"))
          requests.push(req.url());
      };
    admin.on("request", listener);
    try {
      await wait(31000);
      assert.equal(requests.length, 0);
      await admin.click('.monitoring-refresh button[aria-label="Refresh"]');
      await settled(admin);
      assert.ok(requests.length >= 2);
      await clickText(admin, "button", "Resume refresh");
      await see(admin, "Refreshes every 30 seconds", ".monitoring-status");
    } finally {
      admin.off("request", listener);
    }
  },
);

check(
  "pagination, page size and filtering work with a long production-shaped usage history",
  async () => {
    const intervals = Array.from({ length: 61 }, (_, i) => ({
      name: `history-${String(i).padStart(3, "0")}`,
      kind: "DevMachine",
      opened_at: "2026-09-01T10:00:00Z",
      closed_at: "2026-09-01T11:00:00Z",
      seconds: 3600,
      gpu: 1,
      storage_gib: 20,
      open: false,
    }));
    await intercept(
      customer,
      async (req) => {
        if (new URL(req.url()).pathname != "/papi/usage") return false;
        await req.respond({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            tenant: "tenant-arise",
            as_of: new Date().toISOString(),
            gpu_hours: 61,
            gpu_allocated_now: 0,
            storage_gib_hours: 1220,
            storage_gib_now: 0,
            intervals,
            interval_count: 61,
            note: "",
          }),
        });
        return true;
      },
      async () => {
        await route(customer, "usage");
        await settled(customer);
        await see(customer, "history-000", "table");
        await customer.click('.pagination button[aria-label="Next page"]');
        await see(customer, "history-015", "table");
        assert.ok(!(await text(customer, "table")).includes("history-000"));
        await customer.select('select[aria-label="Rows per page"]', "50");
        assert.equal(await customer.$$eval("tbody tr", (es) => es.length), 50);
        await see(customer, "history-000", "table");
        await fill(
          customer,
          'input[aria-label="Search resource name"]',
          "history-060",
        );
        await settled(customer);
        await see(customer, "history-060", "table");
        assert.equal(await customer.$$eval("tbody tr", (es) => es.length), 1);
        await customer.screenshot({
          path: resolve(OUT, "usage-pagination-filter.png"),
        });
      },
    );
  },
);

check(
  "a custom CPU draft keeps its specification after closing and reopening creation",
  async () => {
    await route(customer, "dev");
    await openCreate(customer);
    const catalog = await request(customer, "/papi/flavors");
    const flavor = catalog.body.flavors.find(
      (f) => f.vcpu === null && f.gpu === 0,
    );
    assert.ok(flavor);
    await customer.click(`.fcard[data-flavor="${flavor.key}"]`);
    await fillNumber(customer, 'input[aria-label="vCPU (whole cores)"]', 12);
    await fillNumber(customer, 'input[aria-label="Memory GiB (integer)"]', 48);
    await closeDrawer(customer);
    await openCreate(customer);
    assert.equal(
      await customer.$eval(
        'input[aria-label="vCPU (whole cores)"]',
        (e) => e.value,
      ),
      "12",
    );
    assert.equal(
      await customer.$eval(
        'input[aria-label="Memory GiB (integer)"]',
        (e) => e.value,
      ),
      "48",
    );
    assert.equal(
      await customer.$eval(`.fcard[data-flavor="${flavor.key}"]`, (e) =>
        e.getAttribute("aria-pressed"),
      ),
      "true",
    );
    await closeDrawer(customer);
  },
);

check(
  "blocked browser preference storage does not break login or theme switching",
  async () => {
    const p = await newPage();
    try {
      await p.evaluateOnNewDocument(() => {
        Storage.prototype.getItem = () => {
          throw new DOMException("blocked", "SecurityError");
        };
        Storage.prototype.setItem = () => {
          throw new DOMException("blocked", "SecurityError");
        };
      });
      await p.goto(base);
      await p.waitForSelector("input[type=password]");
      await p.click(".theme-toggle");
      assert.equal(await p.$eval("html", (e) => e.dataset.theme), "dark");
      await fill(p, "input[type=text]", "admin");
      await fill(
        p,
        "input[type=password]",
        process.env.GW_ADMIN_PASSWORD || "arise-admin",
      );
      await p.click("button[type=submit]");
      await p.waitForSelector(".page-heading");
      await p.click(".theme-toggle");
      assert.equal(await p.$eval("html", (e) => e.dataset.theme), "light");
      assert.deepEqual(errors, []);
    } finally {
      await p.close();
    }
  },
);

check(
  "partial fleet telemetry outage is unavailable rather than zero alerts or empty audit",
  async () => {
    const live = await request(admin, "/oapi/fleet");
    assert.equal(live.status, 200);
    const response = {
      ...live.body,
      alerts: [],
      events: [],
      errors: { alerts: "unavailable", events: "unavailable" },
    };
    await intercept(
      admin,
      async (req) => {
        if (new URL(req.url()).pathname !== "/oapi/fleet") return false;
        await req.respond({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(response),
        });
        return true;
      },
      async () => {
        await route(admin, "overview");
        await settled(admin);
        const card = await admin.$$eval(
          ".metric-card",
          (cards) =>
            cards.find((card) =>
              card.querySelector(".metric-top")?.textContent.includes("Alerts"),
            )?.innerText,
        );
        assert.ok(card?.includes("Service temporarily unavailable"), card);
        assert.ok(card?.includes("—"), card);
        await route(admin, "audit");
        await see(admin, "Service temporarily unavailable", ".page-error");
        await route(admin, "fleet");
        await settled(admin);
        assert.ok(
          (await admin.$$(".fleet-node")).length > 0,
          "telemetry failure must keep node operations available",
        );
      },
    );
  },
);

check(
  "production-shaped native fleet capacities reach overview and fleet with fractional cores",
  async () => {
    const live = await request(admin, "/oapi/fleet");
    assert.equal(live.status, 200);
    const node = {
      ...live.body.nodes[0],
      vcpuTotal: 287.5,
      vcpuUsed: 1.5,
      memGiTotal: 2250,
      memGiUsed: 2,
    };
    const response = { ...live.body, nodes: [node], errors: {} };
    await intercept(
      admin,
      async (req) => {
        if (new URL(req.url()).pathname !== "/oapi/fleet") return false;
        await req.respond({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(response),
        });
        return true;
      },
      async () => {
        await route(admin, "overview");
        await settled(admin);
        const values = await admin.$$eval(
          ".metrics-grid .arco-statistic",
          (es) => es.map((e) => e.innerText.replace(/\s/g, "")),
        );
        assert.ok(values[0].includes("1.50/287.5"), values);
        assert.ok(values[1].includes("2.00/2250"), values);
        await route(admin, "fleet");
        await settled(admin);
        const card = (await text(admin, ".fleet-node")).replace(/\s/g, "");
        assert.ok(card.includes("1.5/287.5"), card);
        assert.ok(card.includes("2/2250"), card);
      },
    );
  },
);
