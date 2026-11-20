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
        mode = os.environ.get("MODE", "headless")
        log(f"MODE = {mode}")
        args = []
        kw = {}
        ua_kw = {}
        if mode in ("newheadless", "newheadless-flags"):
            kw["channel"] = "chromium"
        if mode.startswith("chrome"):
            kw["channel"] = "chrome"
            kw["headless"] = False
        if mode == "headed":
            kw["headless"] = False
        if mode.endswith("flags") or mode == "headed-flags" or mode.startswith("chrome"):
            args.append("--disable-blink-features=AutomationControlled")
            if not mode.startswith("chrome"):
                ua_kw["user_agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        browser = p.chromium.launch(args=args, **kw)
        context = browser.new_context(storage_state=state, locale="en-US", **ua_kw)
        page = context.new_page()
        log("UA : " + page.evaluate("navigator.userAgent") + " | webdriver=" + str(page.evaluate("navigator.webdriver")))
        page.on("console", lambda m: log(f"CONSOLE {m.type}: {m.text[:300]}") if (m.type in ("error", "warning") and "font-size:0" not in m.text and "GL Driver" not in m.text) or m.text.startswith("TSMSG") else None)
        context.add_init_script("""
          if (window.top === window) {
          window.__ev = [];
          window.addEventListener('message', e => { try { if (e.data && e.data.event && e.data.event !== 'food') window.__ev.push(e.data.event); console.log('TSMSG ' + e.origin + ' ' + JSON.stringify(e.data).slice(0,400)); } catch (x) {} });
          (function wait() {
            if (window.turnstile && !window.__tsw) {
              window.__tsw = true;
              const r = window.turnstile.render;
              window.turnstile.render = function(el, opts) {
                console.log('TSMSG render opts=' + JSON.stringify(Object.keys(opts||{})) + ' appearance=' + (opts||{}).appearance + ' execution=' + (opts||{}).execution + ' size=' + (opts||{}).size);
                const o = Object.assign({}, opts);
                for (const k of ['callback','error-callback','expired-callback','timeout-callback','before-interactive-callback','unsupported-callback']) {
                  if (typeof opts[k] === 'function') { const f = opts[k]; o[k] = function() { console.log('TSMSG cb ' + k + ' ' + JSON.stringify([].slice.call(arguments)).slice(0,200)); return f.apply(this, arguments); }; }
                }
                return r.call(this, el, o);
              };
            }
            if (!window.__tsw) setTimeout(wait, 50);
          })();
          }
        """)
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
        t_wait = time.time()
        while time.time() - t_wait < 25 and "interactiveBegin" not in page.evaluate("window.__ev"):
            page.wait_for_timeout(300)
        log("événements Turnstile : " + str(page.evaluate("window.__ev")))
        for f in page.frames:
            if "cloudflare" in f.url:
                try:
                    el = f.frame_element()
                    box = el.bounding_box()
                    info = el.evaluate("""e => { const out=[]; let n=e; while(n && n.tagName){ const c=getComputedStyle(n); const r=n.getBoundingClientRect(); out.push(n.tagName+'#'+n.id+' disp='+c.display+' vis='+c.visibility+' op='+c.opacity+' pos='+c.position+' '+Math.round(r.width)+'x'+Math.round(r.height)+'@'+Math.round(r.x)+','+Math.round(r.y)); n=n.parentElement; if(out.length>5) break;} return out; }""")
                    log(f"   FRAME box={box} chaine={info}")
                except Exception as e:  # noqa: BLE001
                    log(f"   FRAME erreur {str(e)[:120]}")
        if os.environ.get("CLICK_WIDGET") == "1":
            for f in page.frames:
                if "cloudflare" in f.url:
                    try:
                        box = f.frame_element().bounding_box()
                    except Exception:  # noqa: BLE001
                        box = None
                    if box and box["width"] > 20 and box["height"] > 20:
                        x, y = box["x"] + 28, box["y"] + box["height"] / 2
                        page.mouse.move(x - 120, y - 40, steps=12)
                        page.wait_for_timeout(400)
                        page.mouse.move(x, y, steps=18)
                        page.wait_for_timeout(300)
                        page.mouse.click(x, y)
                        log(f"   clic souris sur le widget en ({x:.0f},{y:.0f})")
                        page.wait_for_timeout(4000)
                        log("   événements après clic : " + str(page.evaluate("window.__ev")))
        marks = [1, 3, 6, 12, 20, 35]
        def snap(tag):
            try:
                page.screenshot(path=f"/tmp/shot-{mode}-{tag}.png", full_page=True)
            except Exception as e:  # noqa: BLE001
                log(f"screenshot KO {e}")
            for f in page.frames:
                if "cloudflare" in f.url:
                    try:
                        log(f"   IFRAME {f.url[:70]} texte={f.locator('body').inner_text(timeout=1500)[:200]!r}")
                    except Exception as e:  # noqa: BLE001
                        log(f"   IFRAME {f.url[:70]} texte illisible: {str(e)[:80]}")
            log("   widgets turnstile : " + str(page.evaluate("""() => [...document.querySelectorAll('[class*=turnstile],[id*=turnstile],iframe')].map(e => e.tagName+'#'+(e.id||'')+'.'+(e.className||'')+' '+JSON.stringify(e.getBoundingClientRect()))""")))
        snap("t0")
        ok = False
        t_click = time.time()
        for m in marks:
            while time.time() - t_click < m:
                page.wait_for_timeout(250)
            txt = page.locator("main").inner_text()[:260].replace("\n", " | ")
            log(f"{m}s après le clic : {txt}")
            if m in (3, 20):
                snap(f"t{m}")
            if re.search(r"Next card|Click the card to reveal|Your booster", page.locator("main").inner_text()) or page.locator('button[aria-label="Reveal the card"]').count():
                log("RESULTAT : ECRAN DES CARTES ATTEINT")
                ok = True
                break
            log("   boutons : " + str([b.inner_text().strip()[:30] for b in page.get_by_role("button").all()][:10]))
        log("RESULTAT : " + ("OK" if ok else "ECHEC (reste sur Opening)"))
        OB.load(page)
        log("après rechargement : " + page.locator("main").inner_text()[:200].replace("\n", " | "))
        browser.close()


if __name__ == "__main__":
    main()
