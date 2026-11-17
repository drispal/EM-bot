"""Ouvre le booster gratuit d'Essec Masters s'il est disponible.

La session de connexion est lue dans la variable d'environnement
ESSEC_STORAGE_STATE (JSON produit par login_local.py). Aucun mot de passe
n'est utilisé ici.

Le résultat est écrit dans les logs ET dans le résumé de l'exécution GitHub
(page de l'exécution, sans avoir à ouvrir les logs).
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

HOME_URL = "https://www.essecmasters.com/home"
END_MARKERS = r"Click the card to reveal it|Next card|Your booster|NEXT BOOSTER IN|See my collection"


def summary(text):
    """Ajoute du texte au résumé de l'exécution GitHub (si disponible)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def collection_count(page):
    m = re.search(r"Collection:\s*(\d+)\s*/\s*(\d+)", page.inner_text("body"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def timer_text(page):
    m = re.search(r"next booster in\s*(\d+:\d+)", page.inner_text("body"), re.I)
    return m.group(1) if m else None


def card_snapshot(page):
    """Texte de la carte affichée (rareté, nom, sous-titre), sur une ligne."""
    text = page.locator("main").inner_text()
    m = re.search(r"ESSEC MASTERS(.*?)(" + END_MARKERS + r"|$)", text, re.S | re.I)
    block = m.group(1) if m else text
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    # retire la ligne d'initiales (ex. "EE") affichée sur les cartes de personnes
    lines = [l for l in lines if not re.fullmatch(r"[A-Z]{1,3}", l) or l in ("EPIC",)]
    return " | ".join(lines)


def click(locator):
    """Clic DOM direct : insensible aux éléments qui recouvrent le bouton."""
    locator.first.dispatch_event("click")


def reveal_card(page):
    needs_reveal = (
        page.get_by_text("Click the card to reveal it").count() > 0
        or page.get_by_role("button", name="Reveal the card").count() > 0
    )
    if needs_reveal:
        click(page.locator("button.pack"))
        page.wait_for_timeout(2500)


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        summary("## Échec\nSecret `ESSEC_STORAGE_STATE` manquant.")
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    state = json.loads(raw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=state, locale="en-US")
        page = context.new_page()
        page.goto(HOME_URL, wait_until="networkidle")

        if "/login" in page.url:
            summary("## Échec\nSession expirée : relance `login_local.py` et mets à jour le secret.")
            sys.exit("Session expirée : relance login_local.py et mets à jour le secret.")

        before = collection_count(page)
        button = page.get_by_role("button", name="Open the booster")
        page.wait_for_timeout(1000)

        if (
            button.count() == 0
            or not button.first.is_enabled()
            or button.first.get_attribute("aria-disabled") == "true"
        ):
            msg = f"Pas de booster disponible (prochain dans {timer_text(page) or '?'})."
            print(msg)
            summary(f"## Pas de booster\n{msg}")
            browser.close()
            return

        click(button)
        page.wait_for_timeout(1500)
        print("Booster ouvert.")

        # Le booster est ouvert (les cartes sont créditées). On parcourt les
        # 3 cartes pour les afficher ; une erreur ici ne fait pas échouer l'exécution.
        cards = []
        previous = None
        try:
            for i in range(1, 4):
                reveal_card(page)
                snap = card_snapshot(page)
                if i > 1 and snap == previous:
                    # le passage à la carte suivante n'a pas eu lieu : on réessaie une fois
                    nxt = page.get_by_role("button", name=re.compile(r"Next card", re.I))
                    if nxt.count():
                        click(nxt)
                        page.wait_for_timeout(1200)
                        reveal_card(page)
                        snap = card_snapshot(page)
                cards.append(snap)
                print(f"Carte {i}: {snap}")
                previous = snap
                if i < 3:
                    nxt = page.get_by_role("button", name=re.compile(r"Next card", re.I))
                    if nxt.count() == 0:
                        break
                    click(nxt)
                    page.wait_for_timeout(1200)
        except Exception as exc:  # noqa: BLE001
            print(f"Affichage des cartes interrompu : {type(exc).__name__}")

        page.wait_for_timeout(1000)
        after = collection_count(page)
        print(f"Collection : {before} -> {after}")

        lines = ["## Booster ouvert"]
        for i, c in enumerate(cards, 1):
            lines.append(f"- **Carte {i}** : {c}")
        if before and after:
            lines.append(f"\nCollection : {before[0]} → {after[0]} / {after[1]} cartes")
        summary("\n".join(lines))
        browser.close()


if __name__ == "__main__":
    main()
