# Browser acceptance

`platform.mjs` drives real Chromium against the deployed **kind** gateway and its actual Kubernetes backends. It creates temporary customer accounts and uniquely named workloads, then removes them. It refuses production contexts, nonempty evidence directories and sealed evidence packs.

Prerequisites: the lab platform is deployed and healthy; the frontend build has been published with `make web-assets`; Node dependencies are installed with `npm ci` in `web`; Chrome/Chromium is available locally. The script uses `BROWSER_PATH` or an existing Puppeteer Chrome cache. It does not download a browser implicitly.

```bash
# From the repository root. This command changes only the lab test fixtures.
BROWSER_PATH=/absolute/path/to/chrome make test-browser

# Optional, new empty output directory and another kind context:
BROWSER_EVIDENCE="$PWD/evidence/RUN-browser-manual-YYYYMMDDTHHMMSSZ/browser" \
KUBE_CONTEXT=kind-b300-prelab BROWSER_PATH=/absolute/path/to/chrome \
npm --prefix web run test:e2e
```

Use `BROWSER_NO_SANDBOX=1` only in an isolated CI environment that cannot start Chromium's sandbox. The default retains the browser sandbox. `GW_ADMIN_PASSWORD` supplies the lab administrator password if it differs from the documented lab default; never put a production credential here.

The full command registers 39 groups covering:

- Administrator navigation across 13 pages, customer navigation across 8 pages, protected links and tenant/API denial.
- Account creation/deletion; development machine, online service, volume and simulated GPU job creation; resource deletion; instance/event drawers, selecting logs from two actual job replicas, and SSH-key error handling.
- Password validation/change, old credential rejection, session expiry and logout failures.
- Explicit partial telemetry outages (unavailable, never zero alerts), native fleet totals, production-shaped GPU/CPU/memory quantities, mixed-unit quota progress, monitoring queries, invalid JSON and backend failures on every data view.
- Keyboard command navigation, overview/runtime launch shortcuts, GPU and replica configuration summaries, resource search/reset, verified SSH command generation, client and CSV downloads, mobile navigation, ownership gate review, and monitoring time-range/keyboard interaction.
- Creation failures preserve the draft and show their error beside the submit action; download content is checked against the real client and displayed allocation records.
- Direct theme switching and reload persistence; blocked preference storage; custom monitoring validation, bounded historical queries, missing-data gaps, pause/manual refresh and metrics export; long-list pagination and custom CPU draft restoration.
- Initial authentication outage and missing lazy asset recovery, catalog retry, English/Chinese switching, dark mode, narrow login and customer workbench layouts.

Only the **explicit failure-injection and production-shaped-data cases** intercept responses. They do not claim to measure a real GPU. The other cases send requests to the real lab gateway. Screenshots, browser version, individual results, page errors and cleanup results go into a new evidence directory. A filtered `node --test --test-name-pattern=...` run is useful for debugging but its report has `complete: false`; only a full successful run qualifies as acceptance.

The suite is not Firefox/WebKit coverage, a visual pixel-diff baseline, a complete accessibility audit, a sustained-load test or public-network TLS/SSH acceptance. Chinese screenshots require CJK fonts on the browser host (for example, Noto Sans CJK SC). If system fonts cannot be changed, point `FONTCONFIG_FILE` at an isolated fontconfig file. The 2026-09-07 visual review used this method; the Latin UI font is self-hosted with the app.

Related independent checks:

```bash
make validate                  # schema, security, controller, billing, provisioning and gate regressions
make test-auth-recovery        # temporary processes: concurrency, SIGKILL, backup/restore, connection limits
python3 tests/live_http_load.py # four workers, 80 read-only lab API requests; a short sample
```

Run workload-mutating cluster suites sequentially. The authentication recovery suite uses only temporary local databases; it can run independently of the lab. The HTTP load sampler is deliberately bounded and reports measured latency without claiming a production SLO.

## Theme and layout quality gate

```bash
# Same deployed kind platform and installed Chrome prerequisites as above.
BROWSER_PATH=/absolute/path/to/chrome make test-browser-quality
```

`quality.mjs` uses a separate loopback tunnel to the explicitly selected kind context. It reads pages and opens UI controls without creating or deleting accounts or workloads. It refuses nonempty/sealed output directories. `BROWSER_EVIDENCE`, `BROWSER_NO_SANDBOX`, `GW_ADMIN_PASSWORD` and `FONTCONFIG_FILE` work as in the business suite.

The gate checks the login, all 13 pages and resource creation drawers in light/dark mode at 1512, 1024, 768, 390 and 320px. It also checks English pages and open environment/time menus, account/custom-time dialogs, command search, account menus, node details, ownership forms and collapsed navigation. Each capture waits for visible loading indicators to clear, then asserts the actual theme on both html and body. Narrow layouts also assert that header actions align and the administrator workspace selector occupies its own row. A full run must inspect all 192 states (80 with axe). It records horizontal overflow, small text, offscreen text and navigation clipping, runs axe-core WCAG A/AA checks on designated states, and captures screenshots for review. A runtime exception or a reported violation fails the command; inspect `results.json` and `reports.json`.

`QUALITY_FOCUS=popups` checks only 20 focused states for debugging; its report has `complete: false` and is not full acceptance.

Axe findings marked **incomplete** retain their element targets and details in the report for manual review. The gate is not a complete WCAG certification, a pixel-diff baseline, Safari/Firefox coverage, a real-device or long-term operational acceptance. Browser screenshots should also be reviewed: a green geometry assertion alone cannot establish visual quality.

Monitoring queries use fixed windows when a custom range is applied. Relative presets extend to 30 days; custom queries allow up to 90 days, with about 600 evaluations per series. This is a query limit, not a retention guarantee: the current manifests retain 3 days in lab and 15 days in dgx. Missing history stays empty on the requested time axis.
