"""Recyclage des doublons + achat des cartes manquantes sur Essec Masters.

1. Recyclage : sur /market/sell, bouton « Recycle N » SOUS chaque carte
   (jamais le bouton général « Recycle N duplicates »). Seules les cartes
   COMMON / RARE / EPIC sont recyclées ; LEGENDARY et CREATOR sont gardées.
   Le jeu garde toujours un exemplaire de chaque carte.
2. Achat : sur /market (annonces à prix fixe), on achète les cartes que
   l'on n'a pas encore (« New for you ») si le prix est STRICTEMENT inférieur
   au seuil de sa rareté : epic < 40, rare < 25, common < 15.

Mode simulation (DRY_RUN) : par défaut, rien n'est cliqué, le script affiche
ce qu'il ferait. Mettre DRY_RUN=false pour agir pour de vrai.
"""
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.essecmasters.com"
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

RARITIES = {"COMMON", "RARE", "EPIC", "LEGENDARY", "CREATOR"}
RECYCLABLE = {"COMMON", "RARE", "EPIC"}  # on garde LEGENDARY et CREATOR
MAX_PRICE = {"epic": 40, "rare": 25, "common": 15}  # achat si prix < seuil
BUY_ORDER = ["epic", "rare", "common"]

MAX_RECYCLES = 100  # garde-fous par exécution
MAX_BUYS = 15
MAX_MORE_CLICKS = 6

report = []
state = {"buys": 0}  # compteur d'achats partagé entre toutes les raretés


def log(msg):
    print(msg)
    report.append(msg)


def write_summary(title):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    mode = "SIMULATION (rien n'a été cliqué)" if DRY_RUN else "ACTIONS RÉELLES"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## {title} : {mode}\n")
        for line in report or ["Rien à faire."]:
            f.write(f"- {line}\n")


# ---------------------------------------------------------------- parsing
def tile_lines(text):
    return [l.strip() for l in text.splitlines() if l.strip()]


def tile_rarity(lines):
    return next((l.upper() for l in lines if l.upper() in RARITIES), None)


def tile_name(lines, rarity):
    """Premier libellé après la rareté (hors ×N, 'New!', 'New for you', 'You have ×N')."""
    after = lines[[l.upper() for l in lines].index(rarity) + 1:]
    for l in after:
        if re.fullmatch(r"×\d+", l) or re.fullmatch(r"new!?|new for you", l, re.I):
            continue
        if re.fullmatch(r"you have ×\d+", l, re.I):
            continue
        return l
    return None


def listing_price(lines):
    for i, l in enumerate(lines):
        if l.lower() == "essecoins" and i > 0 and re.fullmatch(r"[\d\s.,]+", lines[i - 1]):
            return int(re.sub(r"\D", "", lines[i - 1]))
    return None


def parse_listing(text):
    lines = tile_lines(text)
    rarity = tile_rarity(lines)
    if not rarity:
        return None
    return {
        "rarity": rarity,
        "name": tile_name(lines, rarity),
        "is_new": any(l.lower() == "new for you" for l in lines),
        "price": listing_price(lines),
    }


# ------------------------------------------------------------------ pages
def goto(page, path):
    page.goto(BASE + path, wait_until="networkidle")
    if "/login" in page.url:
        report.append("Session expirée : relance login_local.py et mets à jour le secret.")
        write_summary("Marché")
        sys.exit("Session expirée.")
    page.wait_for_timeout(800)


def balance(page):
    m = re.search(r"MY ESSECOINS\s*([\d\s.,]+)", page.locator("main").inner_text(), re.I)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1500)


def press(locator):
    try:
        locator.click(timeout=8000)
    except Exception:  # noqa: BLE001
        locator.dispatch_event("click")


# -------------------------------------------------------------- recyclage
def eligible_recycles(page):
    """Cartes avec un bouton « Recycle N » individuel, de rareté recyclable."""
    tiles = page.locator("main div.grid > div")
    found = []
    for i in range(tiles.count()):
        tile = tiles.nth(i)
        btn = tile.get_by_role("button", name=re.compile(r"^Recycle \d+"))
        if btn.count() == 0 or "duplicates" in btn.first.inner_text():
            continue  # jamais le bouton général
        lines = tile_lines(tile.inner_text())
        rarity = tile_rarity(lines)
        if rarity not in RECYCLABLE:
            continue
        found.append({"idx": i, "rarity": rarity, "name": tile_name(lines, rarity),
                      "label": " ".join(btn.first.inner_text().split())})
    return found


def recycle_all(page):
    goto(page, "/market/sell")
    start = balance(page)
    gain_sim = 0
    skipped = set()
    done = 0
    for _ in range(MAX_RECYCLES):
        items = [it for it in eligible_recycles(page) if it["name"] not in skipped]
        if not items:
            break
        if DRY_RUN:
            for it in items:
                log(f"[SIMULATION] Recycler {it['rarity']} « {it['name']} » ({it['label']})")
                m = re.search(r"\+\s*(\d+)", it["label"])
                gain_sim += int(m.group(1)) if m else 0
            break
        it = items[0]
        before = balance(page)
        press(page.locator("main div.grid > div").nth(it["idx"])
              .get_by_role("button", name=re.compile(r"^Recycle \d+")).first)
        settle(page)
        goto(page, "/market/sell")
        after = balance(page)
        if before is not None and after is not None and after > before:
            done += 1
            log(f"Recyclé {it['rarity']} « {it['name']} » : +{after - before} essecoins")
        else:
            log(f"Recyclage non confirmé pour « {it['name']} » (solde inchangé), ignoré.")
            skipped.add(it["name"])
    end = balance(page) if not DRY_RUN else start
    return (start or 0) + gain_sim if DRY_RUN else (end or 0), start, done


# ------------------------------------------------------------------ achats
def collect_listings(page):
    tiles = page.locator("main div.grid > div").filter(
        has=page.locator("input[name='listingId']"))
    out = []
    for i in range(tiles.count()):
        info = parse_listing(tiles.nth(i).inner_text())
        if info and info["price"] is not None:
            info["idx"] = i
            out.append(info)
    return out


def buy_rarity(page, rarity, budget, bought):
    limit = MAX_PRICE[rarity]
    path = f"/market?rarity={rarity}&kind=fixed&sort=price"
    goto(page, path)
    purchases = 0
    for _ in range(MAX_BUYS):
        if state["buys"] >= MAX_BUYS:
            break
        more_clicks = 0
        while True:
            listings = collect_listings(page)
            cands = [l for l in listings
                     if l["is_new"] and l["rarity"].lower() == rarity
                     and l["price"] < limit and l["price"] <= budget
                     and l["name"] not in bought]
            more = page.get_by_role("button", name="Show more listings")
            last_price = listings[-1]["price"] if listings else None
            if cands or more.count() == 0 or more_clicks >= MAX_MORE_CLICKS \
                    or (last_price is not None and last_price >= limit):
                break
            press(more.first)
            more_clicks += 1
            settle(page)
        if not cands:
            break
        if DRY_RUN:
            for c in cands:
                if state["buys"] >= MAX_BUYS:
                    break
                if c["price"] <= budget and c["name"] not in bought:
                    log(f"[SIMULATION] Acheter {rarity.upper()} « {c['name']} » à {c['price']} essecoins")
                    budget -= c["price"]
                    bought.add(c["name"])
                    purchases += 1
                    state["buys"] += 1
            break
        c = cands[0]
        before = balance(page)
        tile = page.locator("main div.grid > div").filter(
            has=page.locator("input[name='listingId']")).nth(c["idx"])
        press(tile.get_by_role("button", name="Buy", exact=True))
        settle(page)
        goto(page, path)
        after = balance(page)
        if before is not None and after is not None and after < before:
            budget = after
            purchases += 1
            state["buys"] += 1
            bought.add(c["name"])
            log(f"Acheté {rarity.upper()} « {c['name']} » à {before - after} essecoins")
        else:
            log(f"Achat non confirmé pour « {c['name']} » (solde inchangé), ignoré.")
            bought.add(c["name"])
    return budget, purchases


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    state = json.loads(raw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=state, locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())  # confirmations éventuelles

        budget, start, recycled = recycle_all(page)
        bought = set()
        total_buys = 0
        for rarity in BUY_ORDER:
            budget, n = buy_rarity(page, rarity, budget, bought)
            total_buys += n

        goto(page, "/market")
        final = balance(page)
        log(f"Solde : {start} -> {final} essecoins ({recycled} recyclage(s), {total_buys} achat(s))")
        browser.close()
    write_summary("Marché")


if __name__ == "__main__":
    main()
