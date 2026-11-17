"""Ouvre le booster gratuit d'Essec Masters s'il est disponible.

La session de connexion est lue dans la variable d'environnement
ESSEC_STORAGE_STATE (JSON produit par login_local.py). Aucun mot de passe
n'est utilisé ici.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

HOME_URL = "https://www.essecmasters.com/home"


def collection_count(page):
    m = re.search(r"Collection:\s*(\d+)\s*/\s*(\d+)", page.inner_text("body"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def timer_text(page):
    m = re.search(r"next booster in\s*(\d+:\d+)", page.inner_text("body"), re.I)
    return m.group(1) if m else None


def card_snapshot(page):
    """Texte de la carte affichée (entre 'ESSEC MASTERS' et le bouton suivant)."""
    text = page.locator("main").inner_text()
    m = re.search(r"ESSEC MASTERS(.*?)(Next card|NEXT BOOSTER IN|See my collection|$)", text, re.S | re.I)
    block = m.group(1) if m else text
    return " | ".join(line.strip() for line in block.splitlines() if line.strip())


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    state = json.loads(raw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=state, locale="en-US")
        page = context.new_page()
        page.goto(HOME_URL, wait_until="networkidle")

        if "/login" in page.url:
            sys.exit("Session expirée : relance login_local.py et mets à jour le secret.")

        before = collection_count(page)
        button = page.get_by_role("button", name="Open the booster")
        page.wait_for_timeout(1000)

        if (
            button.count() == 0
            or not button.first.is_enabled()
            or button.first.get_attribute("aria-disabled") == "true"
        ):
            print(f"Pas de booster disponible (prochain dans {timer_text(page) or '?'}).")
            browser.close()
            return

        button.first.click()
        page.wait_for_timeout(1500)

        # Parcourt les 3 cartes
        for i in range(1, 4):
            print(f"Carte {i}: {card_snapshot(page)}")
            nxt = page.get_by_role("button", name=re.compile(r"Next card", re.I))
            if nxt.count() == 0:
                break
            nxt.first.click()
            page.wait_for_timeout(1000)

        page.wait_for_timeout(1000)
        after = collection_count(page)
        print(f"Collection : {before} -> {after}")
        browser.close()


if __name__ == "__main__":
    main()
