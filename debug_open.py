"""Diagnostic : ouvre le booster comme le bot et journalise ce que fait le site (requêtes,
réponses, console, texte de la page à différents instants)."""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

import open_booster as OB

T0 = time.time()


def log(msg):
    print(f"[+{time.time() - T0:5.1f}s] {msg}", flush=True)


def main():
    state = json.loads(os.environ["ESSEC_STORAGE_STATE"])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=state, locale="en-US")
        page = context.new_page()
        page.on("console", lambda m: log(f"CONSOLE {m.type}: {m.text[:200]}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: log(f"PAGEERROR {str(e)[:200]}"))
        page.on("requestfailed", lambda r: log(f"REQFAILED {r.method} {r.url[:120]} {r.failure}"))

        def on_response(r):
            req = r.request
            if req.resource_type in ("fetch", "xhr", "document") or req.method != "GET":
                body = ""
                try:
                    if req.resource_type in ("fetch", "xhr") or req.method != "GET":
                        body = r.text()[:300].replace("\n", " ")
                except Exception:  # noqa: BLE001
                    body = "(corps illisible)"
                post = (req.post_data or "")[:120].replace("\n", " ")
                log(f"{req.method} {r.status} {req.resource_type} {r.url[:110]} post={post!r} body={body!r}")

        page.on("response", on_response)
        OB.load(page, OB.HOME_URL)
        log(f"URL après chargement : {page.url}")
        log("PAGE : " + page.locator("main").inner_text()[:260].replace("\n", " | "))
        button = page.get_by_role("button", name="Open the booster")
        page.wait_for_timeout(1000)
        log(f"boutons 'Open the booster' : {button.count()} ; disponible={OB.is_available(button) if button.count() else None}")
        log("iframes : " + str([f.url[:80] for f in page.frames]))
        if not button.count() or not OB.is_available(button):
            log("Pas de booster disponible, rien à cliquer.")
            browser.close()
            return
        OB.click(button)
        log("CLIC sur le booster.")
        for t in (1, 2, 4, 8, 15, 30):
            page.wait_for_timeout(int((t - (time.time() - T0 - 0)) * 0) + 0)
            time.sleep(max(0, t - 0) if False else 0)
        marks = [1, 3, 6, 12, 20, 35]
        t_click = time.time()
        for m in marks:
            while time.time() - t_click < m:
                page.wait_for_timeout(250)
            txt = page.locator("main").inner_text()[:260].replace("\n", " | ")
            log(f"{m}s après le clic : {txt}")
            log("   boutons : " + str([b.inner_text().strip()[:30] for b in page.get_by_role("button").all()][:10]))
        log("iframes : " + str([f.url[:80] for f in page.frames]))
        OB.load(page)
        log("après rechargement : " + page.locator("main").inner_text()[:200].replace("\n", " | "))
        browser.close()


if __name__ == "__main__":
    main()
