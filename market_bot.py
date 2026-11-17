"""Marché Essec Masters : MODE TEST (lecture seule, aucun clic d'achat ni de recyclage).

Calcule et affiche dans les logs et le résumé GitHub :
  1. combien d'essecoins rapporterait le recyclage des doublons,
  2. quelles cartes manquantes seraient achetées (épiques < 40, rares < 25,
     communes < 15 essecoins), dans l'ordre épiques, rares, communes.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.essecmasters.com"
LIMITS = {"EPIC": 40, "RARE": 25, "COMMON": 15}  # achat si prix STRICTEMENT inférieur
ORDER = ["EPIC", "RARE", "COMMON"]
MAX_MORE_CLICKS = 15  # nombre max de « Show more listings » (lecture seule)
RARITIES = ("COMMON", "RARE", "EPIC", "LEGENDARY", "CREATOR")


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def parse_listing(text, href):
    """Extrait rareté, nom, prix et « nouvelle pour toi » d'une annonce."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    rarity = next((l for l in lines if l in RARITIES), None)
    price_m = re.search(r"(\d+)\s*essecoins", text)
    name = None
    if rarity:
        for l in lines[lines.index(rarity) + 1:]:
            if l.startswith("New for you") or l.startswith("You have") or re.fullmatch(r"[A-Z]{1,3}", l):
                continue
            name = l
            break
    return {
        "href": href,
        "rarity": rarity,
        "name": name or "?",
        "price": int(price_m.group(1)) if price_m else None,
        "new": "New for you" in text,
    }


def read_balance(page):
    m = re.search(r"MY ESSECOINS\s*(\d+)", page.locator("main").inner_text())
    return int(m.group(1)) if m else None


def read_recycle(page):
    """(nombre de doublons, essecoins gagnés) d'après le bouton de recyclage."""
    page.goto(BASE + "/market/sell", wait_until="networkidle")
    page.wait_for_timeout(1500)
    balance = read_balance(page)
    btn = page.get_by_role("button", name=re.compile(r"Recycle\s+\d+\s+duplicates", re.I))
    if btn.count() == 0:
        return balance, 0, 0
    label = btn.first.inner_text().replace("\n", " ")
    m = re.search(r"Recycle\s+(\d+)\s+duplicates\D*(\d+)\s*essecoins", label, re.I)
    if not m:
        return balance, 0, 0
    return balance, int(m.group(1)), int(m.group(2))


def read_listings(page):
    page.goto(BASE + "/market", wait_until="networkidle")
    page.wait_for_timeout(1500)
    for _ in range(MAX_MORE_CLICKS):
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            break
        more.first.dispatch_event("click")
        page.wait_for_timeout(1200)

    listings = []
    for btn in page.get_by_role("button", name="Buy", exact=True).all():
        try:
            box = btn.locator("xpath=ancestor::*[.//a[starts-with(@href,'/cards/')]][1]")
            href = box.locator("a[href^='/cards/']").first.get_attribute("href")
            item = parse_listing(box.inner_text(), href)
            item["enabled"] = btn.is_enabled()
            listings.append(item)
        except Exception:  # noqa: BLE001
            continue
    return listings


def plan_purchases(listings, budget):
    cands = [
        l for l in listings
        if l["new"] and l["rarity"] in LIMITS and l["price"] is not None and l["price"] < LIMITS[l["rarity"]]
    ]
    cheapest = {}
    for l in cands:  # une seule annonce par carte : la moins chère
        if l["href"] not in cheapest or l["price"] < cheapest[l["href"]]["price"]:
            cheapest[l["href"]] = l
    ordered = sorted(cheapest.values(), key=lambda l: (ORDER.index(l["rarity"]), l["price"]))
    buy, skipped, left = [], [], budget
    for l in ordered:
        if left >= l["price"]:
            buy.append(l)
            left -= l["price"]
        else:
            skipped.append(l)
    return buy, skipped, left


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
            summary("## Marché (test)\nSession expirée.")
            sys.exit("Session expirée.")

        balance, n_dup, gain = read_recycle(page)
        listings = read_listings(page)
        browser.close()

    warn = ""
    if n_dup and gain > 40 * n_dup:
        warn = "ATTENTION : le gain dépasse 40 par doublon, donc le bouton inclut probablement des légendaires."
    budget = (balance or 0) + gain
    buy, skipped, left = plan_purchases(listings, budget)

    lines = ["## Marché (MODE TEST, rien n'est cliqué)"]
    lines.append(f"- Essecoins actuels : {balance}")
    lines.append(f"- Recyclage : {n_dup} doublon(s) → +{gain} essecoins. {warn}".rstrip())
    lines.append(f"- Budget après recyclage : {budget}")
    lines.append(f"- Annonces lues : {len(listings)} (dont {sum(1 for l in listings if l['new'])} nouvelles pour toi)")
    lines.append("\n**À acheter (dans cet ordre) :**")
    for l in buy:
        lines.append(f"- {l['rarity']} · {l['name']} · {l['price']}")
    if not buy:
        lines.append("- rien")
    if skipped:
        lines.append("\n**Éligibles mais budget insuffisant :**")
        for l in skipped:
            lines.append(f"- {l['rarity']} · {l['name']} · {l['price']}")
    lines.append(f"\nEssecoins restants après achats : {left}")
    out = "\n".join(lines)
    print(out)
    summary(out)


if __name__ == "__main__":
    main()
