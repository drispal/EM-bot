"""Enchères Essec Masters : mise minimale dans la dernière minute.

On surveille les enchères qui se terminent dans les prochaines minutes, pour les
cartes que l'on n'a pas encore (« New for you »), et dont la mise minimale reste
acceptable (même règle que les achats : seuil de la rareté + points par essecoin).
Quand il reste une minute ou moins, on mise le MINIMUM autorisé par le site (le champ
« Your bid » est pré-rempli : environ +5 % de la meilleure mise). Si quelqu'un nous
surenchérit, on remise le minimum tant que cela reste acceptable (4 mises max par
enchère). Une mise est rendue tout de suite si l'on est dépassé.

Interrupteur : AUCTION_DRY_RUN ("false" pour miser pour de vrai, sinon simulation).
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

import market as M

DRY_RUN = M.flag("AUCTION_DRY_RUN")
HORIZON_S = 6 * 60       # on suit les enchères qui finissent dans moins de 6 minutes
BID_WHEN_S = 60          # on mise quand le site affiche 1 min restante (ou moins)
MAX_BIDS = 4             # mises maximum par enchère
DEADLINE_S = 9 * 60      # durée maximale du script
FAR_POLL_S, NEAR_POLL_S = 45, 8

report = []


def log(msg):
    print(msg)
    report.append(msg)


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    mode = "SIMULATION (aucune mise)" if DRY_RUN else "MISES RÉELLES"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## Enchères : {mode}\n")
        for line in report or ["Rien à faire."]:
            f.write(f"- {line}\n")


AUCTIONS_JS = r"""() => [...document.querySelectorAll("main form")]
  .filter(f => f.querySelector("input[name='amount']") && f.querySelector("input[name='listingId']"))
  .map(f => {
    let el = f;
    while (el.parentElement && el.parentElement.tagName !== 'MAIN'
           && el.parentElement.querySelectorAll("input[name='listingId']").length === 1) {
      el = el.parentElement;
    }
    const a = f.querySelector("input[name='amount']");
    return {lid: f.querySelector("input[name='listingId']").value,
            min: parseInt(a.min || a.value || '0', 10), text: el.innerText};
  })"""


def time_left(text):
    """Secondes restantes d'après « ⏱ 15 min left » (None si introuvable)."""
    m = re.search(r"⏱\s*([^\n]*?)\s*left", text)
    if not m:
        return None
    s = m.group(1).lower()
    if "<" in s or "less" in s:
        return 30  # « <1 min left »
    if re.search(r"\d+\s*(d|day)", s) or re.search(r"\d+\s*h", s):
        return 3600 * 2  # « 1 h 04 » : loin d'être terminée
    total = 0
    mm = re.search(r"(\d+)\s*min", s)
    ss = re.search(r"(\d+)\s*(?:s|sec)\b", s)
    if mm:
        total += int(mm.group(1)) * 60
    if ss:
        total += int(ss.group(1))
    return total if (mm or ss) else None


def read_auctions(page):
    M.goto(page, "/market?kind=auction&sort=ending")
    out = []
    for it in page.evaluate(AUCTIONS_JS):
        info = M.parse_listing(it["text"])
        if not info or not info["name"]:
            continue
        info.update({"lid": it["lid"], "min": it["min"], "secs": time_left(it["text"]),
                     "left_text": (re.search(r"⏱[^\n]*", it["text"]) or [""])[0].strip()})
        out.append(info)
    return out


def place_bid(page, lid, amount):
    form = page.locator(f"main form:has(input[name='listingId'][value='{lid}'])").first
    form.locator("input[name='amount']").fill(str(amount))
    M.press(form.get_by_role("button", name="Bid", exact=True))
    M.settle(page)


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    session = json.loads(raw)
    t0 = time.time()
    tracked = {}  # lid -> suivi de l'enchère

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=session, locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())

        budget = None
        while time.time() - t0 < DEADLINE_S:
            auctions = read_auctions(page)
            if budget is None:
                budget = M.balance(page) or 0

            # 1) nouvelles enchères intéressantes qui se terminent bientôt
            for a in auctions:
                if a["lid"] in tracked or a["secs"] is None or a["secs"] > HORIZON_S:
                    continue
                if a["is_new"] and a["rarity"].lower() in M.POINTS \
                        and M.acceptable(a["rarity"], a["min"], budget):
                    tracked[a["lid"]] = {"name": a["name"], "rarity": a["rarity"], "bids": 0,
                                         "last": None, "done": False}
                    log(f"Enchère suivie : {a['rarity']} « {a['name']} », mise minimale {a['min']} "
                        f"({a['left_text']}).")

            # 2) enchères terminées (plus dans la liste, ou plus de compte à rebours)
            live = {a["lid"] for a in auctions if a["secs"] is not None}
            for lid, t in tracked.items():
                if not t["done"] and lid not in live:
                    t["done"] = True
                    log(f"Enchère terminée : « {t['name']} »"
                        + (f" (notre dernière mise : {t['last']})" if t["last"] else "")
                        + ". Résultat à voir dans ton historique.")

            # 3) miser quand il reste 1 minute ou moins
            for a in auctions:
                t = tracked.get(a["lid"])
                if not t or t["done"] or a["secs"] is None or a["secs"] > BID_WHEN_S:
                    continue
                if t["last"] is not None:
                    if a["price"] == t["last"]:
                        continue  # on est déjà en tête
                    budget += t["last"]  # dépassé : la mise nous est rendue
                    t["last"] = None
                if not M.acceptable(a["rarity"], a["min"], budget):
                    t["done"] = True
                    log(f"Enchère abandonnée : « {t['name']} », mise minimale {a['min']} "
                        f"(trop cher ou solde insuffisant : {budget}).")
                    continue
                if t["bids"] >= MAX_BIDS:
                    t["done"] = True
                    log(f"Enchère abandonnée : « {t['name']} » ({MAX_BIDS} mises déjà posées).")
                    continue
                pts = M.POINTS[a["rarity"].lower()]
                if DRY_RUN:
                    t["done"] = True
                    log(f"[SIMULATION] Miser {a['min']} essecoins sur {a['rarity']} « {t['name']} » "
                        f"({pts} pt, affiché : « {a['left_text']} », meilleure mise {a['price']}).")
                    continue
                place_bid(page, a["lid"], a["min"])
                t["bids"] += 1
                t["last"] = a["min"]
                budget -= a["min"]  # mise bloquée tant que l'on est en tête
                log(f"Mise posée : {a['min']} essecoins sur {a['rarity']} « {t['name']} » "
                    f"({pts} pt, affiché : « {a['left_text']} »).")

            active = [t for t in tracked.values() if not t["done"]]
            if not active:
                break
            nearest = min((a["secs"] for a in auctions
                           if a["lid"] in tracked and not tracked[a["lid"]]["done"]
                           and a["secs"] is not None), default=0)
            wait = NEAR_POLL_S if nearest <= BID_WHEN_S + 60 else \
                min(FAR_POLL_S, max(NEAR_POLL_S, nearest - BID_WHEN_S - 45))
            page.wait_for_timeout(wait * 1000)

        if not tracked:
            log("Aucune enchère intéressante dans les prochaines minutes.")
        elif any(not t["done"] for t in tracked.values()):
            log("Délai maximum atteint : certaines enchères n'ont pas été traitées.")
        browser.close()
    write_summary()


if __name__ == "__main__":
    main()
