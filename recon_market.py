"""Repérage en lecture seule des pages Market (aucun clic, aucune action).

Affiche dans les logs la liste des liens du site, puis le texte et les boutons
des pages dont l'adresse contient "market". Sert uniquement à préparer le
script de vente/achat ; sera supprimé ensuite.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.essecmasters.com"


def dump(page, label):
    print(f"\n===== {label} : {page.url} =====")
    main = page.locator("main")
    print("--- TEXTE ---")
    print(main.inner_text()[:5000])
    print("--- BOUTONS ---")
    for el in page.get_by_role("button").all()[:60]:
        try:
            name = (el.get_attribute("aria-label") or el.inner_text() or "").strip().replace("\n", " ")
            print(f"[button] {name[:80]!r} disabled={el.is_disabled()} class={(el.get_attribute('class') or '')[:80]!r}")
        except Exception:  # noqa: BLE001
            pass
    print("--- LIENS ---")
    for a in page.locator("main a[href]").all()[:40]:
        try:
            print(f"[link] {a.get_attribute('href')} | {a.inner_text().strip().replace(chr(10), ' ')[:60]!r}")
        except Exception:  # noqa: BLE001
            pass
    print("--- HTML (début) ---")
    print(main.inner_html()[:3500])


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret manquant.")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US")
        page = context.new_page()
        page.goto(BASE + "/home", wait_until="networkidle")
        if "/login" in page.url:
            sys.exit("Session expirée.")

        hrefs = sorted({a.get_attribute("href") for a in page.locator("a[href^='/']").all() if a.get_attribute("href")})
        print("LIENS DU SITE :", hrefs)

        targets = ["/album"]
        for t in targets[:4]:
            page.goto(BASE + t, wait_until="networkidle")
            page.wait_for_timeout(1500)
            dump(page, f"page {t}")
            # onglets / sous-pages repérés sur cette page (lecture seule : on ne clique pas)
            n = page.get_by_text(re.compile(r"×\s*[2-9]")).count()
            print("ELEMENTS AVEC ×2 ou plus :", n)
            for el in page.get_by_text(re.compile(r"×\s*[2-9]")).all()[:8]:
                try:
                    print("[dup]", el.evaluate("e => (e.closest('a,li,div')||e).innerText").replace("\n", " | ")[:200])
                except Exception:  # noqa: BLE001
                    pass
        browser.close()


if __name__ == "__main__":
    main()
