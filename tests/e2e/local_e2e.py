#!/usr/bin/env python3
"""P-BitM local end-to-end test environment driver.

Exercises the full stack against a live deployment:
  1. admin login + seeded modules (MFA/OTP Relay, Clipboard Hijacker,
     Bot Guard, Fake Cloudflare Challenge)
  2. landing page + target list + standalone campaign creation
  3. victim onboarding via the public lure URL (real Chromium via Playwright)
  4. operator WebSocket stream receiving module_data broadcasts
  5. MFA relay module execution + code collection
  6. clipboard hijack module execution
  7. bot-guard module execution (verdict collection)
  8. session-snapshot trigger against the selkies victim container
  9. CLI `modules list` / `modules data` export

Prerequisites:
  - `python3 p-bitm.py setup && python3 p-bitm.py up --build` completed
  - Playwright + a Chromium binary (`pip install playwright`)
  - admin credentials shown by `p-bitm.py up` after first start

Usage:
  python3 tests/e2e/local_e2e.py <admin_user> <admin_password> [--keep]
"""
import json
import os
import subprocess
import sys
import time
import urllib3

import httpx

BASE = os.environ.get("PBITM_BASE", "https://127.0.0.1:8443")
VERIFY = False  # self-signed local certificate
urllib3.disable_warnings()

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def main():
    pw_user, pw_pass = sys.argv[1], sys.argv[2]
    keep = "--keep" in sys.argv

    client = httpx.Client(verify=VERIFY)
    csrf = {"token": None}

    def api(method, path, **kw):
        if method.upper() not in ("GET", "HEAD", "OPTIONS") and csrf["token"]:
            kw.setdefault("headers", {})["X-CSRF-Token"] = csrf["token"]
        return client.request(method, BASE + path, timeout=30, **kw)

    # 1. login
    r = api("POST", "/api/auth/login", json={"username": pw_user, "password": pw_pass})
    if r.status_code != 200:
        report("admin login", False, f"{r.status_code} {r.text[:200]}")
        return 1
    csrf["token"] = r.json().get("csrf_token")
    report("admin login", True, r.json().get("user", {}).get("username", "?"))

    # 2. seeded modules
    r = api("GET", "/api/modules")
    modules = {m["name"]: m for m in r.json().get("modules", [])}
    expected = ["MFA/OTP Relay", "Clipboard Hijacker", "Bot Guard (Browser Check)",
                "Fake Cloudflare Challenge"]
    missing = [e for e in expected if e not in modules]
    report("modules seeded", not missing,
           f"{len(modules)} modules" + (f", missing: {missing}" if missing else ""))
    mod_ids = {name: modules[name]["id"] for name in expected if name in modules}

    # 3. landing page + target list + campaign
    ts = str(int(time.time()))[-6:]
    r = api("POST", "/api/landing-pages",
            json={"name": f"e2e-landing-{ts}",
                  "content": "<html><body><h1>E2E landing</h1></body></html>"})
    page_id = r.json().get("id")
    report("create landing page", r.status_code in (200, 201), f"{r.status_code}")

    r = api("POST", "/api/target-lists", json={"name": f"e2e-targets-{ts}"})
    list_id = r.json().get("id")
    report("create target list", r.status_code in (200, 201) and bool(list_id), f"{r.status_code}")

    r = api("POST", f"/api/target-lists/{list_id}/targets",
            json={"email": "victim@example.com", "first_name": "Test", "last_name": "Victim"})
    report("add target", r.status_code in (200, 201), f"{r.status_code}")

    r = api("POST", "/api/campaigns", json={
        "name": f"e2e-campaign-{ts}",
        "url": "https://example.com/login",
        "campaign_type": "standalone",
        "landing_page_id": page_id,
        "target_list_id": list_id,
        "launch_type": "immediate",
        "module_ids": list(mod_ids.values()),
    })
    if r.status_code not in (200, 201):
        report("create campaign", False, f"{r.status_code} {r.text[:300]}")
        return 1
    cid = r.json().get("id") or r.json().get("campaign", {}).get("id")
    report("create campaign", bool(cid), f"campaign {cid}")

    deadline = time.time() + 180
    container_up = False
    while time.time() < deadline:
        ps = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True).stdout
        if f"p-bitm-{cid}" in ps:
            container_up = True
            break
        time.sleep(5)
    report("campaign container running", container_up, f"p-bitm-{cid}")

    r = api("GET", f"/api/campaigns/{cid}/victims")
    victims = r.json().get("victims", [])
    if not victims:
        report("victim pre-created", False, r.text[:200])
        return 1
    victim = victims[0]
    vid, tid = victim["id"], victim.get("tracking_id")
    report("victim pre-created", bool(vid and tid), f"victim {vid}")

    r = api("GET", f"/api/campaigns/{cid}")
    camp = r.json()
    camp = camp.get("campaign", camp)
    routes = (camp.get("advanced_options") or {}).get("routes") or {}
    entry_path = routes.get("entry_path") or ""
    public_url = camp.get("public_url") or f"https://192.168.10.70/{cid}/"
    lure = f"{public_url.rstrip('/')}/{entry_path}/{tid}" if entry_path \
        else f"{public_url.rstrip('/')}/{tid}"
    print(f"  lure: {lure}", flush=True)

    # 4. playwright victim
    from playwright.sync_api import sync_playwright
    chromium_path = "/usr/bin/chromium"
    launch_kwargs = {"args": ["--no-sandbox", "--ignore-certificate-errors",
                              "--disable-quic"]}
    if not os.path.exists(chromium_path):
        chromium_path = None  # use Playwright's bundled Chromium
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chromium_path,
            **launch_kwargs,
        )
        ctx = browser.new_context(ignore_https_errors=True)
        # copy the admin session cookie into the browser context so the
        # dashboard-origin pages (operator WS) are authenticated
        ctx.add_cookies([{
            "name": c.name, "value": c.value, "url": BASE,
            "secure": bool(c.secure),
        } for c in client.cookies.jar])
        page = ctx.new_page()
        page.goto(lure, timeout=60000, wait_until="domcontentloaded")

        # wait for WS connect + victim container spawn (first boot is slow)
        active = False
        for _ in range(60):
            time.sleep(3)
            r = api("GET", f"/api/campaigns/{cid}/victims")
            vnow = [v for v in r.json().get("victims", []) if v["id"] == vid][0]
            if vnow.get("is_active"):
                active = True
                break
        report("victim connected", active, f"active={active}")
        if not active:
            browser.close()
            return 1

        # operator WS stream — must run on the dashboard origin (session
        # cookie is scoped to it; the victim page lives on the campaign
        # public origin where /api is not routed)
        dash = ctx.new_page()
        dash.goto(BASE + "/", timeout=30000, wait_until="domcontentloaded")
        dash.evaluate(
            """(cid) => {
                window.__bitmMsgs = [];
                const proto = location.protocol === 'https:' ? 'wss' : 'ws';
                const ws = new WebSocket(`${proto}://${location.host}/api/campaigns/${cid}/stream`);
                ws.onmessage = (e) => { try { window.__bitmMsgs.push(JSON.parse(e.data)); } catch {} };
                window.__bitmWs = ws;
            }""",
            cid,
        )
        dash.wait_for_timeout(2000)
        # the dashboard SPA rotates the CSRF token via /api/auth/me —
        # refresh ours so subsequent operator API calls stay authorized
        r = api("GET", "/api/auth/me")
        if r.status_code == 200 and r.json().get("csrf_token"):
            csrf["token"] = r.json()["csrf_token"]

        def exec_module(module_name, params):
            r = api("POST", f"/api/campaigns/{cid}/victims/{vid}/execute-module",
                    json={"module_id": mod_ids[module_name], "params": params})
            return r.status_code, r.text[:200]

        # 5. MFA relay
        code, txt = exec_module("MFA/OTP Relay", {"0": "E2E Corp", "1": ""})
        report("execute MFA module", code in (200, 201, 204), f"{code} {txt}")
        try:
            page.wait_for_selector("input[inputmode='numeric']", timeout=15000)
            page.fill("input[inputmode='numeric']", "123456")
            page.click("button:has-text('Verify')")
            page.wait_for_timeout(4000)
            mfa_ok = True
        except Exception as e:
            mfa_ok = False
            print("  mfa interaction error:", str(e)[:200], flush=True)
        report("MFA overlay interaction", mfa_ok)

        # 6. clipboard hijack
        code, txt = exec_module("Clipboard Hijacker",
                                {"0": "Your verification code is 999-888",
                                 "1": "powershell evil", "2": "true"})
        report("execute clipboard module", code in (200, 201, 204), f"{code} {txt}")
        try:
            page.wait_for_selector("button:has-text('Copy')", timeout=15000)
            page.click("button:has-text('Copy')")
            page.wait_for_timeout(4000)
            clip_ok = True
        except Exception as e:
            clip_ok = False
            print("  clipboard interaction error:", str(e)[:200], flush=True)
        report("clipboard overlay interaction", clip_ok)

        # 7. bot guard (no decoy -> reveals page after verdict)
        code, txt = exec_module("Bot Guard (Browser Check)", {"0": "", "1": "10"})
        report("execute bot-guard module", code in (200, 201, 204), f"{code} {txt}")
        page.wait_for_timeout(12000)

        r = api("GET", f"/api/campaigns/{cid}/victims/{vid}/modules-data")
        text = r.text
        report("MFA code collected", "123456" in text)
        report("clipboard result collected", "planted" in text)
        report("bot-guard verdict collected", '"verdict"' in text)
        try:
            for g in r.json():
                items = g.get("data") or g.get("items") or []
                for item in items:
                    meta = item.get("metadata") or item.get("extra_metadata") or {}
                    if isinstance(meta, dict) and meta.get("verdict"):
                        print(f"  bot-guard verdict: {meta['verdict']} "
                              f"score={meta.get('score')}", flush=True)
        except Exception:
            pass

        msgs = dash.evaluate("window.__bitmMsgs || []")
        md_msgs = [m for m in msgs if m.get("type") == "module_data"]
        report("operator WS broadcast", len(md_msgs) >= 2,
               f"{len(md_msgs)} module_data events")

        page.screenshot(path="tests/e2e/victim-page.png")

        # 8. snapshot (victim container stays up while the WS is open)
        r = api("POST", f"/api/campaigns/{cid}/victims/{vid}/capture-snapshot")
        req_id = r.json().get("request_id") if r.status_code in (200, 201) else None
        report("trigger capture-snapshot", r.status_code in (200, 201),
               f"{r.status_code} {r.text[:150]}")
        if req_id:
            snap_ok = False
            for _ in range(60):
                time.sleep(3)
                r = api("GET", f"/api/campaigns/{cid}/victims/{vid}/screenshots")
                if req_id in r.text:
                    snap_ok = True
                    break
            report("snapshot artifact received", snap_ok)
            if not snap_ok:
                ps = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                    capture_output=True, text=True).stdout
                vcont = [n for n in ps.splitlines() if cid in n and f"p-bitm-{cid}" not in n]
                print(f"  victim containers: {vcont}", flush=True)
                for name in vcont:
                    logs = subprocess.run(["docker", "logs", name, "--tail", "30"],
                                          capture_output=True, text=True)
                    print(f"  --- {name} ---\n{logs.stdout[-1500:]}", flush=True)

        if keep:
            input("browser kept open, press Enter to close...")
        browser.close()

    # 9. CLI export
    out = subprocess.run([sys.executable, "p-bitm.py", "modules", "list"],
                         capture_output=True, text=True)
    cli_ok = "MFA/OTP Relay" in out.stdout
    report("CLI modules list", cli_ok,
           out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[:150])
    out = subprocess.run([sys.executable, "p-bitm.py", "modules", "data",
                          "--campaign", cid, "--out", "tests/e2e/export"],
                         capture_output=True, text=True)
    detail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[:200]
    export_ok = out.returncode == 0
    try:
        data = json.load(open("tests/e2e/export/module_data.json"))
        n = len(data if isinstance(data, list) else data.get("rows", data.get("data", [])))
        export_ok = export_ok and n >= 2
        detail = f"{n} rows exported"
    except Exception as e:
        detail = f"export read error: {e}"
    report("CLI modules data export", export_ok, detail)

    print("\n=== SUMMARY ===")
    fails = [n for n, ok, _ in RESULTS if not ok]
    for n, ok, d in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {n}: {d}")
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
