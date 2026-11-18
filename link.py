"""Compte nourrisseur (« Joss ») au service du compte principal.

Deux rôles, dans le même fichier :

* Côté PRINCIPAL (`main_link`, appelé par market.py avant le recyclage des doublons) :
  1. propose à Joss un échange : 1 de tes doublons de peu de valeur contre jusqu'à 5 cartes
     de Joss qui te manquent (les plus rentables d'abord, légendaires comprises) ;
  2. met en vente une carte « appât » (un doublon de peu de valeur) à un prix égal au solde
     d'essecoins de Joss, pour que Joss l'achète et te transfère ses essecoins.
  JAMAIS de légendaire (ni de carte Créateur) donnée ou mise en vente pour Joss.
* Côté JOSS (`feeder_main`, lancé par feeder.py avec la session de Joss) :
  accepte les échanges venant du compte principal (si rien de précieux n'est demandé en
  retour), achète l'annonce appât, et publie son solde dans feeder.json (branche tracking).

Échanges avec Joss et appât sont simulés tant que LINK_DRY_RUN / FEEDER_DRY_RUN ne valent
pas « false ».
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

import market as M

LINK_DRY_RUN = M.flag("LINK_DRY_RUN") or M.DRY_RUN  # côté principal
FEEDER_DRY_RUN = M.flag("FEEDER_DRY_RUN")  # côté Joss
TRACK_DIR = os.environ.get("TRACK_DIR", "data")
FEEDER_NAME = os.environ.get("FEEDER_NAME", "Josserand")  # début du nom de Joss, vu du principal
# nom du compte principal, vu de Joss (échanges reçus, vendeur de l'annonce appât)
MAIN_NAME_RE = re.compile(os.environ.get("FEEDER_MAIN_NAME") or r"^(Damien|DJ)\b", re.I)

NEVER = {"LEGENDARY", "CREATOR"}            # jamais donnés ni vendus pour Joss
PTS = {"COMMON": 1, "RARE": 3, "EPIC": 8, "LEGENDARY": 20, "CREATOR": 0}
JUNK_ORDER = ["COMMON", "RARE", "EPIC"]     # doublons à donner, du moins précieux au plus précieux
MAX_GET = 5
TRADE_PENDING_MIN = 25                      # une proposition sans réponse après 25 min est abandonnée
BAIT_MIN = int(os.environ.get("BAIT_MIN", "60"))     # solde minimum de Joss pour valoir un appât
BAIT_MAX = int(os.environ.get("BAIT_MAX", "500"))    # prix maximal d'un appât (plafond du site à vérifier)
FEEDER_MAX_AGE_MIN = 30                     # feeder.json plus vieux : on ne s'en sert pas
MAX_ACCEPT = 5


# ----------------------------------------------------------------- échanges
TRADE_CARDS_JS = """(side) => [...document.querySelectorAll(`main label:has(input[name='${side}'])`)]
  .map(l => l.innerText)"""


def parse_trade_card(text):
    lines = M.tile_lines(text)
    rarity = M.tile_rarity(lines)
    if not rarity:
        return None
    m = next((re.search(r"×\s*(\d+)", l) for l in lines if re.search(r"×\s*\d+", l)), None)
    return {
        "rarity": rarity,
        "name": M.tile_name(lines, rarity),
        "is_new": any(l.lower() == "new for you" for l in lines),
        "copies": int(m.group(1)) if m else 1,
    }


def trade_cards(page, side):
    out = []
    for i, text in enumerate(page.evaluate(TRADE_CARDS_JS, side)):
        c = parse_trade_card(text)
        if c and c["name"]:
            c["idx"] = i
            out.append(c)
    return out


def friend_href(page, prefix):
    """Lien « Propose a trade » de l'ami dont le nom commence par `prefix` (page /market/trades)."""
    links = page.evaluate("""() => [...document.querySelectorAll('main a[href*="trades/new"]')]
        .map(a => ({text: a.innerText.replace(/\\s+/g, ' ').trim(), href: a.getAttribute('href')}))""")
    for l in links:
        if prefix.lower() in l["text"].lower():
            return l["href"]
    return None


def past_trade_status(text, feeder, names):
    """Statut (Done / Declined...) de l'échange passé avec `feeder` portant ces cartes, sinon None."""
    lines = M.tile_lines(text)
    for i, l in enumerate(lines):
        if l.lower().startswith(feeder.lower()) and " · +" in l and all(n in l for n in names):
            return lines[i + 1] if i + 1 < len(lines) else "?"
    return None


def read_json(name):
    try:
        with open(os.path.join(TRACK_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(name, data):
    os.makedirs(TRACK_DIR, exist_ok=True)
    with open(os.path.join(TRACK_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------- côté principal
def pick_junk(items, min_value_pts):
    """Doublon à donner : le moins précieux, jamais légendaire ni Créateur ; un doublon épique
    seulement si ce qu'on reçoit vaut au moins 16 points."""
    ok = [it for it in items
          if it["rarity"] not in NEVER and it["rarity"] in JUNK_ORDER and not it["on_market"]]
    ok.sort(key=lambda it: JUNK_ORDER.index(it["rarity"]))
    for it in ok:
        if it["rarity"] == "EPIC" and min_value_pts < 16:
            continue
        return it
    return None


def trade_step(page, link, now, reserved=()):
    # 1. proposition en attente ?
    tr = link.get("trade")
    if tr:
        M.goto(page, "/market/trades")
        status = past_trade_status(page.locator("main").inner_text(), FEEDER_NAME, tr["get"])
        age = int((now - tr["t"]) / 60)
        if status:
            M.log(f"Échange avec Joss ({', '.join(tr['get'])}) : {status}.")
            link.pop("trade")
        elif age >= TRADE_PENDING_MIN:
            M.log(f"Échange avec Joss sans réponse après {age} min : abandonné.")
            link.pop("trade")
        else:
            M.log(f"Échange avec Joss en attente depuis {age} min.")
            return False
    # 2. nouvelle proposition
    M.goto(page, "/market/trades")
    href = friend_href(page, FEEDER_NAME)
    if not href:
        M.log(f"Échange : ami « {FEEDER_NAME} » introuvable.")
        return False
    M.goto(page, href)
    wanted = sorted([c for c in trade_cards(page, "get") if c["is_new"]],
                    key=lambda c: -PTS.get(c["rarity"], 0))[:MAX_GET]
    if not wanted:
        M.log("Échange : rien de nouveau chez Joss pour l'instant.")
        return False
    value = sum(PTS.get(c["rarity"], 0) for c in wanted)
    M.goto(page, "/market/sell")
    junk = pick_junk([it for it in M.scan_sell(page) if it["name"] not in reserved], value)
    if not junk:
        M.log("Échange : aucun doublon de peu de valeur à donner pour l'instant.")
        return False
    names = [c["name"] for c in wanted]
    txt = (f"Échange avec Joss : je donne {junk['rarity']} « {junk['name']} », "
           f"je reçois {', '.join(f'{c['rarity']} {c['name']}' for c in wanted)} (≈ +{value} points)")
    if LINK_DRY_RUN:
        M.log(f"[SIMULATION] {txt}")
        return False
    M.goto(page, href)
    give = {c["name"]: c for c in trade_cards(page, "give")}
    if junk["name"] not in give:
        M.log(f"Échange : « {junk['name']} » absent de la liste à donner, abandon.")
        return False
    get = {c["name"]: c for c in trade_cards(page, "get")}
    for n in names:
        if n in get:
            page.locator("main label:has(input[name='get'])").nth(get[n]["idx"]).click()
    page.locator("main label:has(input[name='give'])").nth(give[junk["name"]]["idx"]).click()
    ticked = page.locator("main input[name='get']:checked").count(), page.locator(
        "main input[name='give']:checked").count()
    if ticked[1] != 1 or not 1 <= ticked[0] <= MAX_GET:
        M.log(f"Échange : sélection inattendue {ticked}, abandon (rien envoyé).")
        return False
    M.press(page.get_by_role("button", name="Send the proposal", exact=True))
    M.settle(page)
    link["trade"] = {"t": now, "get": names, "give": junk["name"]}
    M.log(f"{txt} : proposition envoyée.")
    return True


def feeder_alive(fb, now):
    """Joss a publié son solde récemment (son job tourne et accepte les échanges)."""
    return bool(fb) and now - fb.get("at", 0) <= FEEDER_MAX_AGE_MIN * 60


def bait_step(page, data, link, now):
    fb = read_json("feeder.json")
    bait_listed = [e for e in data["listings"] if e.get("bait")]
    # annonce appât terminée (vendue, retirée ou expirée) ?
    if link.get("bait_open") and not bait_listed:
        link["bait_open"] = False
        link["bait_closed_at"] = now
        write_json("bait.json", {"name": None})
        M.log("Annonce appât terminée (vendue ou retirée).")
    if bait_listed:
        return None
    if not feeder_alive(fb, now):
        return None
    if fb["at"] <= link.get("bait_closed_at", 0):
        return None  # on attend un solde de Joss publié après le dernier appât
    balance = int(fb.get("balance", 0))
    if balance < BAIT_MIN:
        return None
    price = min(balance, BAIT_MAX)
    M.goto(page, "/market/sell")
    reserved = {link["trade"]["give"]} if link.get("trade") else set()
    items = [it for it in M.scan_sell(page) if it["name"] not in reserved]
    junk = pick_junk(items, 0)
    if not junk or junk["rarity"] == "EPIC":
        M.log("Appât : aucun doublon commun ou rare à mettre en vente.")
        return None
    txt = f"Appât : {junk['rarity']} « {junk['name']} » à {price} essecoins (solde de Joss : {balance})"
    if LINK_DRY_RUN:
        M.log(f"[SIMULATION] {txt}")
        return junk["name"]
    M.list_for_sale(page, junk, price)
    data["listings"].append({"name": junk["name"], "price": price, "listed_at": now, "bait": True})
    link["bait_open"] = True
    write_json("bait.json", {"name": junk["name"], "rarity": junk["rarity"], "price": price,
                             "listed_at": now})
    M.log(f"{txt} : mise en vente.")
    return junk["name"]


def main_link(page):
    """Appelé par market.py (compte principal) avant le recyclage des doublons."""
    try:
        data = M.load_state()
        link = data.setdefault("link", {})
        before = json.dumps(data, sort_keys=True)
        now = time.time()
        if not LINK_DRY_RUN and not feeder_alive(read_json("feeder.json"), now):
            M.log("Compte nourrisseur : Joss n'a pas publié son solde récemment, rien proposé.")
            return
        # l'appât (des essecoins) passe avant l'échange : il réserve son doublon, l'échange prend un autre
        bait = bait_step(page, data, link, now)
        trade_step(page, link, now, reserved={bait} if bait else ())
        if json.dumps(data, sort_keys=True) != before:
            M.save_state(data)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001  (le compte nourrisseur ne doit jamais bloquer le marché)
        M.log(f"Compte nourrisseur : erreur ignorée ({type(exc).__name__}: {str(exc)[:120]}).")


# ------------------------------------------------------------ côté Joss
INCOMING_JS = r"""() => {
  const accepts = r => [...r.querySelectorAll('button')].filter(x => x.innerText.trim() === 'Accept the trade').length;
  return [...document.querySelectorAll('main button')]
    .filter(b => b.innerText.trim() === 'Accept the trade')
    .map(b => {
      let el = b;
      while (el.parentElement && el.parentElement.tagName !== 'MAIN' && accepts(el.parentElement) <= 1) {
        el = el.parentElement;
        if (/offers you a trade/i.test(el.innerText)) break;
      }
      return el.innerText;
    });
}"""


def parse_incoming(text):
    """{sender, get: [cartes reçues par Joss], give: [cartes données par Joss]}."""
    lines = M.tile_lines(text)
    sender = next((re.sub(r"\s*offers you a trade.*", "", l, flags=re.I)
                   for l in lines if re.search(r"offers you a trade", l, re.I)), "")
    up = [l.upper() for l in lines]
    i_get = up.index("YOU GET") if "YOU GET" in up else -1
    i_give = up.index("YOU GIVE") if "YOU GIVE" in up else -1

    def cards(seg):
        out, seg_lines = [], list(seg)
        for k, l in enumerate(seg_lines):
            if l.upper() in M.RARITIES:
                name = M.tile_name(seg_lines[k:], l.upper())
                if name:
                    out.append({"rarity": l.upper(), "name": name})
        return out

    end_get = i_give if i_give > i_get else len(lines)
    return {"sender": sender,
            "get": cards(lines[i_get + 1:end_get]) if i_get >= 0 else [],
            "give": cards(lines[i_give + 1:]) if i_give >= 0 else []}


def accept_trades(page):
    done = 0
    for _ in range(MAX_ACCEPT):
        M.goto(page, "/market/trades")
        texts = page.evaluate(INCOMING_JS)
        todo = None
        for i, t in enumerate(texts):
            tr = parse_incoming(t)
            if MAIN_NAME_RE.search(tr["sender"] or ""):
                todo = (i, tr)
                break
            M.log(f"Échange de « {tr['sender']} » ignoré (pas le compte principal).")
        if not todo:
            break
        i, tr = todo
        recv = ", ".join(f"{c['rarity']} {c['name']}" for c in tr["get"]) or "rien"
        sent = ", ".join(f"{c['rarity']} {c['name']}" for c in tr["give"]) or "rien"
        bad = any(c["rarity"] in NEVER for c in tr["get"]) or len(tr["get"]) > 2 \
            or len(tr["give"]) > MAX_GET or not tr["give"]
        btn = "Decline" if bad else "Accept the trade"
        if FEEDER_DRY_RUN:
            M.log(f"[SIMULATION] {'Refuser' if bad else 'Accepter'} l'échange : Joss reçoit {recv} "
                  f"et donne {sent}.")
            break
        page.get_by_role("button", name=btn, exact=True).nth(i).click()
        M.settle(page)
        done += 1
        M.log(f"{'Échange refusé' if bad else 'Échange accepté'} : Joss reçoit {recv} et donne {sent}.")
    return done


def buy_bait(page):
    bait = read_json("bait.json")
    if not bait or not bait.get("name"):
        return False
    bal = M.balance(page)
    price, name = int(bait["price"]), bait["name"]
    if bal is None or price > bal:
        M.log(f"Appât « {name} » à {price} : solde de Joss insuffisant ({bal}).")
        return False
    if bait.get("rarity", "").upper() in NEVER:
        M.log("Appât refusé : légendaire / Créateur (jamais).")
        return False
    M.goto(page, M.market_path(bait["rarity"].lower(), name))
    for _ in range(4):
        texts = page.evaluate(M.LISTINGS_JS)
        for i, t in enumerate(texts):
            info = M.parse_listing(t)
            m = re.search(r"sold by\s+(.+)", t, re.I)
            seller = m.group(1).strip().splitlines()[0] if m else ""
            if info and info["name"] == name and info["price"] == price \
                    and MAIN_NAME_RE.search(seller):
                if FEEDER_DRY_RUN:
                    M.log(f"[SIMULATION] Acheter l'appât « {name} » à {price} (vendeur {seller}).")
                    return False
                M.press(page.locator(M.BUY_FORMS).nth(i).get_by_role("button", name="Buy", exact=True))
                M.settle(page)
                M.log(f"Appât acheté : « {name} » à {price} essecoins (vendeur {seller}).")
                return True
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            break
        M.show_more(page, more)
    M.log(f"Appât « {name} » à {price} pas (encore) visible sur le marché.")
    return False


def feeder_main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE (compte Joss) manquant.")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())
        try:
            accept_trades(page)
            buy_bait(page)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            M.log(f"Joss : erreur ignorée ({type(exc).__name__}: {str(exc)[:120]}).")
        M.goto(page, "/market")
        bal = M.balance(page)
        if bal is not None:
            write_json("feeder.json", {"balance": bal, "at": time.time()})
            M.log(f"Solde de Joss : {bal} essecoins.")
        browser.close()
    M.write_summary("Joss (compte nourrisseur)")
