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

from playwright.sync_api import Error as PlaywrightError, sync_playwright

HOME_URL = "https://www.essecmasters.com/home"
MAX_WAIT_SECONDS = 330  # attend le booster s'il arrive dans moins de 5 min 30
END_MARKERS = r"Click the card to reveal it|Next card|Your booster|NEXT BOOSTER IN|See my collection"


def summary(text):
    """Ajoute du texte au résumé de l'exécution GitHub (si disponible)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def load(page, url=None):
    """Charge (ou recharge) la page. Le site est parfois lent : « networkidle » n'est plus
    exigé (10 s au plus) et un chargement échoué est retenté une fois."""
    for attempt in range(2):
        try:
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            break
        except PlaywrightError:
            if attempt == 1:
                raise
            print("Chargement échoué, nouvel essai.")
            page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightError:
        pass


def collection_count(page):
    m = re.search(r"Collection:\s*(\d+)\s*/\s*(\d+)", page.inner_text("body"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def timer_text(page):
    m = re.search(r"next booster in\s*(\d+:\d+)", page.inner_text("body"), re.I)
    return m.group(1) if m else None


def timer_seconds(page):
    t = timer_text(page)
    if not t:
        return None
    minutes, seconds = t.split(":")
    return int(minutes) * 60 + int(seconds)


def is_available(button):
    return (
        button.count() > 0
        and button.first.is_enabled()
        and button.first.get_attribute("aria-disabled") != "true"
    )


BACK_RE = re.compile(r"BOOSTER\s*·\s*\d+\s*CARDS", re.I)  # dos de carte / écran du pack


def card_snapshot(page):
    """Texte de la carte affichée (rareté, nom, sous-titre), sur une ligne.
    Renvoie None si l'écran n'affiche pas (encore) une carte."""
    text = page.locator("main").inner_text()
    m = re.search(r"ESSEC MASTERS(.*?)(" + END_MARKERS + r")", text, re.S | re.I)
    if not m or "Opening" in m.group(1) or BACK_RE.search(m.group(1)):
        return None  # pas (encore) une carte révélée
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip()]
    # retire la ligne d'initiales (ex. "EE") affichée sur les cartes de personnes
    lines = [l for l in lines if not re.fullmatch(r"[A-Z]{1,3}", l)]
    return " | ".join(lines)[:250] or None


def wait_for_cards(page):
    """Attend la fin de l'écran « Opening… » : une carte (ou son dos) apparaît."""
    page.wait_for_function(
        """() => {
            const t = document.body.innerText;
            return t.includes('Next card') || t.includes('Click the card to reveal it')
                || t.includes('Your booster')
                || !!document.querySelector('button[aria-label="Reveal the card"]');
        }""",
        timeout=30000,
    )


def read_card(page):
    """Révèle la carte courante et la lit ; réessaie tant que c'est le dos."""
    for _ in range(6):
        reveal_card(page)
        snap = card_snapshot(page)
        if snap:
            return snap
        page.wait_for_timeout(1000)
    return None


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
        load(page, HOME_URL)

        if "/login" in page.url:
            summary("## Échec\nSession expirée : relance `login_local.py` et mets à jour le secret.")
            sys.exit("Session expirée : relance login_local.py et mets à jour le secret.")

        before = collection_count(page)
        button = page.get_by_role("button", name="Open the booster")
        page.wait_for_timeout(1000)

        if not is_available(button):
            remaining = timer_seconds(page)
            if remaining is not None and remaining <= MAX_WAIT_SECONDS:
                print(f"Booster dans {remaining}s : j'attends.")
                page.wait_for_timeout((remaining + 5) * 1000)
                load(page)
                page.wait_for_timeout(1000)
                before = collection_count(page)
                button = page.get_by_role("button", name="Open the booster")

        if not is_available(button):
            msg = f"Pas de booster disponible (prochain dans {timer_text(page) or '?'})."
            print(msg)
            summary(f"## Pas de booster\n{msg}")
            browser.close()
            return

        click(button)
        print("Clic sur le booster.")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write("opened=true\n")
        try:
            wait_for_cards(page)
        except Exception:  # noqa: BLE001
            print("Écran des cartes non détecté ; état de la page :")
            print(page.locator("main").inner_text()[:300].replace("\n", " | "))
        page.wait_for_timeout(1000)

        # Le booster est ouvert (les cartes sont créditées). On parcourt les
        # 3 cartes pour les afficher ; une erreur ici ne fait pas échouer l'exécution.
        cards = []
        previous = None
        try:
            for i in range(1, 4):
                snap = read_card(page)
                if snap is None:
                    print(f"Carte {i}: non lue")
                    break
                if i > 1 and snap == previous:
                    # le passage à la carte suivante n'a pas eu lieu : on réessaie une fois
                    nxt = page.get_by_role("button", name=re.compile(r"Next card", re.I))
                    if nxt.count():
                        click(nxt)
                        page.wait_for_timeout(1200)
                        snap = read_card(page) or snap
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

        # Le compteur de la page ne se met à jour qu'après un rechargement.
        try:
            load(page)
            page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
        after = collection_count(page)
        print(f"Collection : {before} -> {after}")

        lines = ["## Booster ouvert"]
        if not cards:
            lines.append("Booster ouvert, mais les cartes n'ont pas pu être lues (voir les logs).")
        for i, c in enumerate(cards, 1):
            lines.append(f"- **Carte {i}** : {c}")
        if before and after:
            lines.append(f"\nCollection : {before[0]} → {after[0]} / {after[1]} cartes")
        summary("\n".join(lines))
        browser.close()


if __name__ == "__main__":
    main()
