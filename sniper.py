"""Sniper Essec Masters : achète tout de suite les cartes qui me manquent dès qu'elles
apparaissent au marché (prix fixe), sans attendre le passage du bot principal.

Principe : toutes les SNIPER_POLL_S secondes, on lit les annonces LES PLUS RÉCENTES de chaque
rareté (tri « Newest », une ou deux pages de 24) et on achète toute annonce marquée
« New for you » (carte que je n'ai pas). Quatre pages légères par tour, au lieu de relire
tout le marché. Les enchères restent gérées par auctions.py.

Un run reste vivant SNIPER_MINUTES minutes (5 h 35 par défaut) ; le cron externe relance
un run quand le précédent est fini (le groupe de concurrence met le suivant en attente).

Filet de sécurité (« balayage ») : au démarrage puis toutes les SNIPER_SWEEP_MINUTES minutes
(60 par défaut), le sniper lit dans l'album la liste exacte des cartes qui me manquent (les cartes
grisées, « Not packed yet ») et cherche chacune par son nom au marché, à n'importe quel prix (dans
la limite du plafond de sécurité). Il rattrape ce que la lecture des annonces récentes rate : carte
listée trop chère pour mon solde de l'époque, listée entre deux runs, tour en erreur.

Garde-fous : jamais plus que le solde ; prix maximum SNIPER_COINS_PER_PT essecoins par point
(500 : commune 500, rare 1 500, épique 4 000, légendaire 10 000 ; 0 = aucun plafond) ;
une même carte n'est pas rachetée pendant 10 minutes ; arrêt si la session a expiré.

Interrupteur : SNIPER_DRY_RUN ("false" pour acheter pour de vrai, sinon simulation).
"""
import os
import re
import sys
import time
from collections import Counter

from playwright.sync_api import Error as PlaywrightError, sync_playwright

import market as M

DRY_RUN = M.flag("SNIPER_DRY_RUN")
MINUTES = float(os.environ.get("SNIPER_MINUTES", "335"))
POLL_S = float(os.environ.get("SNIPER_POLL_S", "10"))              # pause entre deux tours
PAGES = max(1, int(os.environ.get("SNIPER_PAGES", "2")))           # pages de 24 annonces lues par rareté
COINS_PER_PT = int(os.environ.get("SNIPER_COINS_PER_PT", "500"))   # 0 = aucun plafond
RESERVE = int(os.environ.get("SNIPER_RESERVE", "0"))               # essecoins jamais dépensés
RARITIES = ("legendary", "epic", "rare", "common")                 # les plus lourdes en points d'abord
TEST_BUY = os.environ.get("SNIPER_TEST_BUY", "") == "1"                # test : un seul achat d'une commune déjà possédée
SWEEP_MINUTES = float(os.environ.get("SNIPER_SWEEP_MINUTES", "60"))  # 0 = pas de balayage
RECENT = 10 * 60                                                   # pas de rachat de la même carte
MAX_ERRORS = 5                                                     # erreurs de suite avant de relancer la page

stats = Counter()
bought = {}   # nom -> instant du dernier achat
lines = []    # journal court pour le résumé


def log(msg):
    stamp = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{stamp}Z] {msg}", flush=True)
    lines.append(f"{stamp}Z · {msg}")


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    mode = "SIMULATION (rien n'est acheté)" if DRY_RUN else "ACHATS RÉELS"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"## Sniper : {mode}\n")
        f.write(f"{stats['rounds']} tour(s) · {stats['seen_new']} annonce(s) de cartes manquantes vue(s) · "
                f"{stats['bought']} achat(s) · {stats['missed']} raté(s) · {stats['errors']} erreur(s)\n\n")
        for line in lines[-60:]:
            f.write(f"- {line}\n")


def load(page, rarity):
    """Ouvre la première page des annonces les plus récentes de cette rareté."""
    page.goto(f"{M.BASE}/market?rarity={rarity}&kind=fixed&sort=new", wait_until="domcontentloaded", timeout=30000)
    if "/login" in page.url:
        log("Session expirée : relance login_local.py et mets à jour le secret ESSEC_STORAGE_STATE.")
        write_summary()
        sys.exit("Session expirée.")
    try:
        page.wait_for_selector(M.BUY_FORMS, timeout=6000)
    except PlaywrightError:
        pass  # aucune annonce de cette rareté, ou chargement lent


IDS_JS = r"""() => [...document.querySelectorAll("main form")]
  .filter(f => f.querySelector("input[name='listingId']")
    && [...f.querySelectorAll('button')].some(b => b.innerText.trim() === 'Buy'))
  .map(f => f.querySelector("input[name='listingId']").value)"""


def listing_ids(page):
    """Identifiants d'annonce, dans le même ordre que M.collect_listings (formulaires « Buy »)."""
    return page.evaluate(IDS_JS)


def read_newest(page, rarity):
    load(page, rarity)
    for _ in range(PAGES - 1):
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            break
        M.show_more(page, more)
    listings = M.collect_listings(page)
    ids = listing_ids(page)
    for i, l in enumerate(listings):
        l["lid"] = ids[i] if i < len(ids) else ""
    return [l for l in listings if l["rarity"].lower() == rarity]


def limit_for(rarity, budget):
    cap = COINS_PER_PT * M.POINTS[rarity] if COINS_PER_PT > 0 else budget
    return min(budget - RESERVE, cap)


def buy(page, rarity, cand, budget, require_new=True, reload=None):
    """Achète l'annonce `cand` ; renvoie le prix payé ou None. La page est relue juste avant le
    clic : l'ordre des annonces peut avoir bougé (une nouvelle annonce décale tout)."""
    name, price, lid = cand["name"], cand["price"], cand["lid"]
    lim = limit_for(rarity, budget)
    pts = M.POINTS[rarity]
    if price > lim:
        log(f"Trop cher pour moi : {rarity.upper()} « {name} » à {price} (limite {lim}, solde {budget}).")
        stats["skipped"] += 1
        return None
    if time.time() - bought.get(name, 0) < RECENT:
        return None
    if DRY_RUN:
        log(f"[SIMULATION] Acheter {rarity.upper()} « {name} » à {price} essecoins ({pts} pt).")
        bought[name] = time.time()
        return price
    cur = [x for x in M.collect_listings(page)
           if x["name"] == name and (x["is_new"] or not require_new) and x["price"] <= lim]
    ids = listing_ids(page)
    cur = [x for x in cur if x["idx"] < len(ids) and ids[x["idx"]] == lid]
    if not cur:
        log(f"Annonce « {name} » déjà partie avant le clic.")
        stats["missed"] += 1
        return None
    M.press(page.locator(M.BUY_FORMS).nth(cur[0]["idx"]).get_by_role("button", name="Buy", exact=True))
    M.settle(page)
    bought[name] = time.time()
    # vérification : l'annonce doit avoir disparu du marché une fois achetée
    (reload or (lambda: load(page, rarity)))()
    if lid in listing_ids(page):
        log(f"⚠ {rarity.upper()} « {name} » à {price} : l'annonce est toujours là après le clic (achat refusé ?).")
        stats["missed"] += 1
        return None
    log(f"ACHETÉ {rarity.upper()} « {name} » à {price} essecoins ({pts} pt).")
    stats["bought"] += 1
    return price


ALBUM_RARITIES = ("legendary", "epic", "rare", "common")
RARITY_WORDS = {"COMMON", "RARE", "EPIC", "LEGENDARY", "CREATOR"}


def parse_missing(text):
    """(nombre annoncé, [(rareté, nom), ...]) d'après la section « Not packed yet (N) » de l'album."""
    m = re.search(r"Not packed yet \((\d+)\)", text)
    if not m:
        return None, []
    lines = [x.strip() for x in text[m.end():].split("\n") if x.strip()]
    out = []
    for i, x in enumerate(lines):
        if x == "?" and i + 2 < len(lines) and lines[i + 1] in RARITY_WORDS:
            out.append((lines[i + 1].lower(), lines[i + 2]))
    return int(m.group(1)), out


def album_missing(page):
    """Cartes qui me manquent, lues dans l'album (une page par rareté, défilement jusqu'en bas
    car la liste se charge par morceaux)."""
    result, problems = [], []
    for rarity in ALBUM_RARITIES:
        page.goto(f"{M.BASE}/album?f={rarity}", wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            log("Session expirée.")
            write_summary()
            sys.exit("Session expirée.")
        expected, items, last_h, still = None, [], -1, 0
        for _ in range(90):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(500)
            expected, items = parse_missing(page.evaluate("document.body.innerText"))
            if expected is not None and len(items) >= expected:
                break
            h = page.evaluate("document.body.scrollHeight")
            still = still + 1 if h == last_h else 0
            last_h = h
            if still >= 6:  # plus rien ne se charge
                break
        if expected is not None and len(items) < expected:
            problems.append(f"{rarity} : {len(items)} nom(s) lus sur {expected}")
        result += [(r, n) for r, n in items if r == rarity]
    return result, problems


def load_search(page, rarity, name):
    page.goto(f"{M.BASE}{M.market_path(rarity, name)}", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(M.BUY_FORMS, timeout=5000)
    except PlaywrightError:
        pass  # aucune annonce pour ce nom


def sweep(page):
    """Cherche au marché, par son nom, chaque carte qui me manque ; achète l'annonce la moins chère."""
    missing, problems = album_missing(page)
    per = Counter(r for r, _ in missing)
    log(f"Balayage : {len(missing)} carte(s) manquante(s) lue(s) dans l'album "
        f"({', '.join(f'{per[r]} {r}' for r in ALBUM_RARITIES if per[r])}).")
    for p in problems:
        log(f"⚠ Lecture de l'album incomplète : {p}")
    budget = None
    found = 0
    for rarity, name in missing:
        load_search(page, rarity, name)
        if budget is None:
            budget = M.balance(page) or 0
        listings = [x for x in M.collect_listings(page) if x["name"] == name and x["is_new"]]
        ids = listing_ids(page)
        for x in listings:
            x["lid"] = ids[x["idx"]] if x["idx"] < len(ids) else ""
        if not listings:
            continue
        found += 1
        stats["seen_new"] += 1
        cand = min(listings, key=lambda x: x["price"])
        log(f"Balayage : {rarity.upper()} « {name} » en vente à {cand['price']} essecoins.")
        paid = buy(page, rarity, cand, budget, reload=lambda: load_search(page, rarity, name))
        if paid is not None:
            budget -= paid
    stats["sweeps"] += 1
    log(f"Balayage terminé : {found} carte(s) manquante(s) trouvée(s) au marché sur {len(missing)}.")


def one_round(page):
    budget = None
    for rarity in RARITIES:
        listings = read_newest(page, rarity)
        if budget is None:
            budget = M.balance(page) or 0
        if stats["rounds"] == 0:  # premier tour : permet de contrôler le tri et la lecture dans le journal
            head = ", ".join(f"{l['price']}{'*' if l['is_new'] else ''}" for l in listings[:8])
            log(f"Lecture {rarity} : {len(listings)} annonce(s), prix des plus récentes (* = me manque) : {head}")
        if TEST_BUY and rarity == "common" and not stats["test_done"]:
            owned = sorted((l for l in listings if not l["is_new"] and l["name"] and l["price"] <= 10),
                           key=lambda l: l["price"])
            if owned:
                stats["test_done"] += 1
                log(f"TEST : achat d'une commune déjà possédée ({owned[0]['name']}, {owned[0]['price']}).")
                paid = buy(page, rarity, owned[0], budget, require_new=False)
                if paid is not None:
                    budget -= paid
        new = sorted((l for l in listings if l["is_new"] and l["name"]), key=lambda l: l["price"])
        done = set()
        for cand in new:
            if cand["name"] in done:
                continue
            done.add(cand["name"])
            stats["seen_new"] += 1
            paid = buy(page, rarity, cand, budget)
            if paid is not None:
                budget -= paid
    stats["rounds"] += 1


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    import json
    deadline = time.time() + MINUTES * 60
    log(f"Démarrage : {'SIMULATION' if DRY_RUN else 'ACHATS RÉELS'}, {MINUTES:.0f} min, "
        f"pause {POLL_S:.0f} s, {PAGES} page(s) par rareté, plafond {COINS_PER_PT or 'aucun'} essecoins/pt, "
        f"balayage toutes les {SWEEP_MINUTES:.0f} min.")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        # le texte suffit : on n'a pas besoin des images ni des polices (pages plus légères et plus rapides)
        context.route("**/*", lambda r: r.abort()
                      if r.request.resource_type in ("image", "font", "media") else r.continue_())
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())
        errors = 0
        next_sweep = time.time() if SWEEP_MINUTES > 0 else float("inf")
        try:
            while time.time() < deadline:
                t0 = time.time()
                try:
                    if time.time() >= next_sweep:
                        next_sweep = time.time() + SWEEP_MINUTES * 60
                        sweep(page)
                    one_round(page)
                    errors = 0
                except SystemExit:
                    raise
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    stats["errors"] += 1
                    log(f"Tour ignoré ({type(exc).__name__}: {str(exc)[:120]}).")
                    if errors >= MAX_ERRORS:
                        log("Trop d'erreurs de suite : nouvelle page.")
                        try:
                            page.close()
                        except Exception:  # noqa: BLE001
                            pass
                        page = context.new_page()
                        page.on("dialog", lambda d: d.accept())
                        errors = 0
                if stats["rounds"] and stats["rounds"] % 60 == 0:
                    log(f"En vie : {stats['rounds']} tours, {stats['bought']} achat(s), {stats['errors']} erreur(s).")
                time.sleep(max(1.0, POLL_S - (time.time() - t0)))
        finally:
            log(f"Fin : {stats['rounds']} tours, {stats['bought']} achat(s), {stats['missed']} raté(s), "
                f"{stats['errors']} erreur(s).")
            write_summary()
            browser.close()


if __name__ == "__main__":
    main()
