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
MAX_MORE_CLICKS = 6

report = []
state = {"halt": False}  # passe à True si aucune action n'apparaît dans l'historique
pending = []  # actions cliquées, à vérifier dans l'historique


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


def history_amounts(page, kind, name):
    """Montants (signés) des lignes « <kind> · <name> » de l'historique d'essecoins.
    L'historique est à jour tout de suite, contrairement au solde affiché."""
    return amounts_in(history_text(page), kind, name)


def history_text(page):
    page.goto(BASE + "/market/mine", wait_until="networkidle")
    page.wait_for_timeout(800)
    return page.locator("main").inner_text()


def amounts_in(text, kind, name):
    pat = rf"{re.escape(kind)} · {re.escape(name)}\s*\n\s*\d{{1,2}}/\d{{1,2}}/\d{{4}}\s*\n\s*([+\-−–]\s*\d+)"
    out = []
    for m in re.findall(pat, text):
        out.append(int(re.sub(r"[^\d]", "", m)) * (-1 if m.strip()[0] in "-−–" else 1))
    return out


def confirm_action(page, kind, name, count_before):
    """Attend qu'une nouvelle ligne d'historique apparaisse. Renvoie son montant ou None."""
    for _ in range(8):
        amounts = history_amounts(page, kind, name)
        if len(amounts) > count_before:
            return amounts[0]  # la plus récente est en haut
        page.wait_for_timeout(2000)
    return None


def add_pending(kind, name, n_before):
    pending.append({"kind": kind, "name": name, "n_before": n_before, "confirmed": False})


def check_pending(page):
    """Relit l'historique une fois et marque les actions devenues visibles."""
    text = history_text(page)
    for p in pending:
        if not p["confirmed"] and len(amounts_in(text, p["kind"], p["name"])) > p["n_before"]:
            p["confirmed"] = True
    return sum(p["confirmed"] for p in pending)


def guard(page):
    """Le site met parfois du temps à afficher l'historique. Si, après plusieurs actions,
    aucune n'y est visible, on arrête (les clics ne semblent pas avoir d'effet)."""
    waiting = [p for p in pending if not p["confirmed"]]
    if len(waiting) >= 3 and check_pending(page) == 0:
        log("Aucune des dernières actions n'apparaît dans l'historique : arrêt du marché.")
        state["halt"] = True


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
        n_before = len(history_amounts(page, "Duplicate recycled", it["name"]))
        goto(page, "/market/sell")
        # l'index peut avoir changé : on retrouve la carte par son nom
        items = [x for x in eligible_recycles(page) if x["name"] == it["name"]]
        if not items:
            skipped.add(it["name"])
            continue
        it = items[0]
        press(page.locator("main div.grid > div").nth(it["idx"])
              .get_by_role("button", name=re.compile(r"^Recycle \d+")).first)
        settle(page)
        gain = confirm_action(page, "Duplicate recycled", it["name"], n_before)
        goto(page, "/market/sell")
        skipped.add(it["name"])  # jamais deux fois la même carte dans un run
        done += 1
        if gain is not None and gain > 0:
            log(f"Recyclé {it['rarity']} « {it['name']} » : +{gain} essecoins")
            add_pending("Duplicate recycled", it["name"], n_before)
            pending[-1]["confirmed"] = True
        else:
            m = re.search(r"\+\s*(\d+)", it["label"])
            log(f"Recyclage envoyé {it['rarity']} « {it['name']} » (≈ +{m.group(1) if m else '?'} essecoins), "
                "pas encore visible dans l'historique.")
            add_pending("Duplicate recycled", it["name"], n_before)
            guard(page)
            if state["halt"]:
                break
    end = balance(page) if not DRY_RUN else start
    return (start or 0) + gain_sim if DRY_RUN else (end or 0), start, done


# ------------------------------------------------------------------ achats
BUY_FORMS = "main form:has(input[name='listingId']):has(button:text-is('Buy'))"

LISTINGS_JS = """() => [...document.querySelectorAll("main form")]
  .filter(f => f.querySelector("input[name='listingId']")
    && [...f.querySelectorAll('button')].some(b => b.innerText.trim() === 'Buy'))
  .map(f => {
    let el = f;
    while (el.parentElement && el.parentElement.tagName !== 'MAIN'
           && el.parentElement.querySelectorAll("input[name='listingId']").length === 1) {
      el = el.parentElement;
    }
    return el.innerText;
  })"""


def collect_listings(page):
    out = []
    for i, text in enumerate(page.evaluate(LISTINGS_JS)):
        info = parse_listing(text)
        if info and info["price"] is not None:
            info["idx"] = i
            out.append(info)
    return out


def locate_listing(page, path, name, limit):
    """Recharge la liste et retrouve l'annonce « New for you » de cette carte."""
    goto(page, path)
    for _ in range(MAX_MORE_CLICKS + 1):
        cur = [x for x in collect_listings(page)
               if x["name"] == name and x["is_new"] and x["price"] < limit]
        if cur:
            return cur[0]
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            return None
        press(more.first)
        settle(page)
    return None


def buy_rarity(page, rarity, budget, bought):
    limit = MAX_PRICE[rarity]
    path = f"/market?rarity={rarity}&kind=fixed&sort=price"
    goto(page, path)
    purchases = 0
    while True:  # s'arrête quand plus rien d'achetable (budget, seuil, échecs)
        if state["halt"]:
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
                if c["price"] <= budget and c["name"] not in bought:
                    log(f"[SIMULATION] Acheter {rarity.upper()} « {c['name']} » à {c['price']} essecoins")
                    budget -= c["price"]
                    bought.add(c["name"])
                    purchases += 1
            break
        c = cands[0]
        n_before = len(history_amounts(page, "Card bought", c["name"]))
        name_c = c["name"]
        c = locate_listing(page, path, name_c, limit)
        if c is None:
            bought.add(name_c)  # plus disponible (vendue ou déjà possédée)
            continue
        press(page.locator(BUY_FORMS).nth(c["idx"]).get_by_role("button", name="Buy", exact=True))
        settle(page)
        paid = confirm_action(page, "Card bought", c["name"], n_before)
        bought.add(c["name"])
        add_pending("Card bought", c["name"], n_before)
        purchases += 1
        if paid is not None and paid < 0:
            pending[-1]["confirmed"] = True
            budget -= -paid
            log(f"Acheté {rarity.upper()} « {c['name']} » à {-paid} essecoins")
        else:
            budget -= c["price"]  # on suppose l'achat passé (prix de l'annonce)
            log(f"Achat envoyé {rarity.upper()} « {c['name']} » à {c['price']} essecoins, "
                "pas encore visible dans l'historique.")
            guard(page)
        goto(page, path)
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

        if pending:  # vérification finale : l'historique peut avoir du retard
            for attempt in range(4):
                ok = check_pending(page)
                if ok == len(pending):
                    break
                if attempt < 3:
                    page.wait_for_timeout(20000)
            log(f"Vérification finale : {ok}/{len(pending)} action(s) visibles dans l'historique.")
            for p_ in pending:
                if not p_["confirmed"]:
                    log(f"  ⚠ « {p_['name']} » ({p_['kind']}) toujours absent de l'historique.")
        goto(page, "/market")
        final = balance(page)
        log(f"Solde : {start} -> {final} essecoins ({recycled} recyclage(s), {total_buys} achat(s))")
        browser.close()
    write_summary("Marché")


if __name__ == "__main__":
    main()
