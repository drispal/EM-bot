"""Lecture seule de mon compte Essec Masters, pour analyse hors du navigateur.

Ne clique que sur « Show more cards » / « Show more listings » (jamais sur un bouton d'action : pas de Buy,
Bid, Cancel, Withdraw, Send...). Écrit le texte brut de chaque page dans le journal du run, entre des
marqueurs « ===== PAGE <chemin> ===== », puis un résumé de l'album.
Chemins lus par défaut : /album (toutes raretés), /market/trades, /market/mine, /friends.
INSPECT_PATHS : chemins supplémentaires séparés par des espaces. Les pages de conditions (/terms...) sont refusées.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.essecmasters.com"
DENY = ("/terms", "/privacy", "/legal")
ALBUM = ["/album", "/album?f=common", "/album?f=rare", "/album?f=epic", "/album?f=legendary", "/album?f=creator"]
PAGES = ["/market/trades", "/market/mine", "/friends"]


def expand(page):
    """Dépile « Show more ... » jusqu'au bout."""
    for _ in range(80):
        more = page.get_by_role("button", name=re.compile(r"^show more", re.I))
        if more.count() == 0:
            return
        before = len(page.inner_text("main"))
        try:
            more.first.click(timeout=8000)
        except Exception:  # noqa: BLE001
            return
        try:
            page.wait_for_function("n => document.querySelector('main').innerText.length > n", arg=before, timeout=8000)
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(800)


def dump(page, path):
    if any(path.startswith(d) for d in DENY):
        print(f"===== PAGE {path} ===== (refusée)")
        return ""
    page.goto(BASE + path, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    expand(page)
    text = page.inner_text("main")
    print(f"===== PAGE {path} =====")
    print(text)
    print(f"===== FIN {path} =====")
    return text


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    extra = [p if p.startswith("/") else "/" + p for p in os.environ.get("INSPECT_PATHS", "").split()]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.dismiss())
        for path in ALBUM + PAGES + extra:
            try:
                dump(page, path)
            except Exception as exc:  # noqa: BLE001
                print(f"===== ERREUR {path} : {type(exc).__name__}: {str(exc)[:150]} =====")
        browser.close()


if __name__ == "__main__":
    main()
