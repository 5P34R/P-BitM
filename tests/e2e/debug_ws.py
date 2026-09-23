#!/usr/bin/env python3
import sys, time
from playwright.sync_api import sync_playwright

lure = sys.argv[1]
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox", "--ignore-certificate-errors", "--disable-quic"])
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    page.on("console", lambda m: print("CONSOLE", m.type, m.text[:300], flush=True))
    page.on("pageerror", lambda e: print("PAGEERROR", str(e)[:300], flush=True))
    page.on("requestfailed", lambda r: print("REQFAIL", r.url[:120], r.failure, flush=True))
    page.on("websocket", lambda ws: print("WS", ws.url, flush=True))
    page.goto(lure, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(15000)
    browser.close()
