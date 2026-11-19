"""Marché Essec Masters : vente / recyclage des doublons, achat des cartes manquantes.

1. Doublons (sur /market/sell, bouton « Recycle N » SOUS chaque carte, jamais le
   bouton général « Recycle N duplicates »). Seules les COMMON / RARE / EPIC sont
   concernées ; LEGENDARY et CREATOR ne sont jamais recyclées ni vendues (sauf sur le compte
   nourrisseur Joss, qui recycle tous ses doublons). Une carte qui
   a déjà un exemplaire en vente ou en échange n'est pas recyclée.
   - COMMON : recyclées (5 essecoins).
   - RARE et EPIC : mises en vente à prix fixe (bien plus que les 15 / 40 du recyclage) ;
     si la carte n'est pas vendue après SELL_TIMEOUT_MIN minutes, l'annonce est retirée
     puis la carte recyclée. Le script ne touche JAMAIS aux annonces qu'il n'a pas créées
     lui-même (la liste est gardée dans sell_state.json).
2. Achat-revente : une annonce d'une carte que l'on possède déjà, vendue bien en dessous
   de sa valeur de recyclage, est achetée ; l'exemplaire en double suit ensuite le circuit
   normal (recyclage, ou mise en vente pour les rares et épiques) au passage suivant.
3. Achat : cartes que l'on n'a pas encore (« New for you »), à prix fixe. On vise la
   valeur au classement (commune 1 pt, rare 3, épique 8, légendaire 20) : on achète
   d'abord les meilleurs « points par essecoin », sous les seuils de la rareté
   (common < 15, rare < 25, epic <= 70, legendary <= 150) et si le ratio est >= MIN_RATIO.
   Ensuite le ratio minimum est assoupli (RELAXED_RATIO : épique <= 100, légendaire <= 250)
   pour ne jamais laisser dormir des essecoins ; au-delà de FLOOR_BALANCE, dernier palier
   (FLOOR_RATIO). On n'attend une meilleure carte que si elle est à moins de HOLD_AHEAD.
   Si une carte intéressante n'est pas encore abordable, on met de côté plutôt que de
   dépenser pour des cartes moins rentables.
   Surplus : le solde au-dessus de SURPLUS_RESERVE (200) achète les communes et rares qui manquent
   jusqu'à SURPLUS_COINS_PER_PT (100) essecoins par point (épiques : 80), meilleurs ratios d'abord.

Interrupteurs (variables d'environnement) :
  DRY_RUN          "false" pour agir pour de vrai, sinon simulation (rien n'est cliqué).
  SELL_DRY_RUN     "false" en plus pour mettre réellement les doublons en vente ; sinon
                   la mise en vente est seulement simulée et les doublons sont recyclés.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError, sync_playwright

BASE = "https://www.essecmasters.com"


def flag(name, default="true"):
    """Interrupteur : vrai sauf si la variable vaut exactement « false »."""
    return os.environ.get(name, default).strip().lower() != "false"


DRY_RUN = flag("DRY_RUN")
SELL_DRY_RUN = DRY_RUN or flag("SELL_DRY_RUN")
FEEDER = flag("FEEDER", "false")  # compte nourrisseur (Joss) : il ne fait que vendre / recycler

RARITIES = {"COMMON", "RARE", "EPIC", "LEGENDARY", "CREATOR"}
RECYCLABLE = {"COMMON", "RARE", "EPIC"}  # on garde LEGENDARY et CREATOR
# Joss (compte nourrisseur) recycle tous ses doublons, légendaires et Créateur compris
RECYCLABLE_JOSS = RECYCLABLE | {"LEGENDARY", "CREATOR"}

# --- achats : valeur au classement (points par carte différente) ----------------
POINTS = {"common": 1, "rare": 3, "epic": 8, "legendary": 20}
# achat normal si prix < seuil : common <= 14, rare <= 24, epic <= 70, legendary <= 150
MAX_PRICE = {"common": 15, "rare": 25, "epic": 71, "legendary": 151}
MIN_RATIO = float(os.environ.get("MIN_RATIO", "0.125"))  # points/essecoin minimum
MIN_RATIO_BY_RARITY = {"epic": 0.11}  # épique : jusqu'à 70 essecoins (8 pts / 70 = 0,114)
RELAXED_RATIO = float(os.environ.get("RELAXED_RATIO", "0.08"))  # tout solde : épique <= 100, légendaire <= 250
IDLE_BALANCE = int(os.environ.get("IDLE_BALANCE", "0"))  # solde à partir duquel on assouplit (0 = toujours)
RESERVE = int(os.environ.get("RESERVE", "0"))  # essecoins gardés lors de l'assouplissement
# si les essecoins s'accumulent malgré tout (rien d'achetable), dernier palier : ratio plancher
FLOOR_RATIO = float(os.environ.get("FLOOR_RATIO", "0.05"))  # épique <= 160, légendaire <= 400
FLOOR_BALANCE = int(os.environ.get("FLOOR_BALANCE", "200"))
# Paliers progressifs (épiques et légendaires seulement : les communes et rares restent plafonnées) :
# plus les essecoins dorment, plus on accepte de payer cher une carte qui manque. Chaque palier
# s'applique tant que le solde reste au-dessus de son seuil, donc il retombe dès qu'on a dépensé.
# (solde minimum, points/essecoin minimum) -> épique / légendaire au plus :
#   200 : 0,05  (160 / 400)   350 : 0,035  (228 / 571)   500 : 0,025  (320 / 800)
IDLE_TIERS = [(FLOOR_BALANCE, FLOOR_RATIO),
              (int(os.environ.get("TIER2_BALANCE", "350")), float(os.environ.get("TIER2_RATIO", "0.035"))),
              (int(os.environ.get("TIER3_BALANCE", "500")), float(os.environ.get("TIER3_RATIO", "0.025")))]
# Surplus : au-dessus d'une réserve, les essecoins qui dorment servent à acheter les communes et
# rares qui manquent, jusqu'à SURPLUS_COINS_PER_PT essecoins par point (commune <= 100, rare <= 300),
# et les épiques jusqu'à SURPLUS_EPIC_COINS_PER_PT (80, soit 640),
# du meilleur « points par essecoin » au moins bon. La réserve n'est jamais entamée par ces achats :
# elle garde de quoi acheter une épique ou une légendaire si elle apparaît (paliers ci-dessus).
SURPLUS_COINS_PER_PT = int(os.environ.get("SURPLUS_COINS_PER_PT", "100"))
SURPLUS_EPIC_COINS_PER_PT = int(os.environ.get("SURPLUS_EPIC_COINS_PER_PT", "80"))  # épique : jusqu'à 640 essecoins
SURPLUS_RESERVE = int(os.environ.get("SURPLUS_RESERVE", "200"))
SURPLUS_RARITIES = ("common", "rare", "epic")
MAX_SURPLUS_BUYS = int(os.environ.get("MAX_SURPLUS_BUYS", "24"))  # garde-fou par exécution (chaque achat prend quelques secondes)
HOLD_AHEAD = 40  # on attend une meilleure carte si elle est à moins de 40 essecoins (~12 min de revenus)
# si les essecoins dorment, ces raretés n'ont plus de plafond de prix (seul le ratio compte)
IDLE_UNCAPPED = {"epic", "legendary"}


def worth(rarity, price, min_ratio=None, idle=False):
    """Prix acceptable pour cette rareté : sous le plafond (sauf épique / légendaire quand
    `idle`) ET assez de points par essecoin."""
    r = rarity.lower()
    if r not in POINTS or price <= 0:
        return False
    ratio = MIN_RATIO_BY_RARITY.get(r, MIN_RATIO) if min_ratio is None else min_ratio
    under_cap = price < MAX_PRICE[r] or (idle and r in IDLE_UNCAPPED)
    return under_cap and POINTS[r] / price >= ratio


def acceptable(rarity, price, budget):
    """Prix acceptable compte tenu du solde : règle normale ; règle assouplie (ratio 0,08,
    épique <= 100) quand le solde le permet ; dernier palier (ratio 0,05) si le solde
    dépasse FLOOR_BALANCE, pour ne jamais laisser dormir les essecoins."""
    if price > budget:
        return False
    if worth(rarity, price):
        return True
    if budget >= IDLE_BALANCE and budget - price >= RESERVE \
            and worth(rarity, price, RELAXED_RATIO, idle=True):
        return True
    return any(budget >= b and worth(rarity, price, r, idle=True) for b, r in IDLE_TIERS)


def price_limits(ratio, idle=False):
    return {r: max((p for p in range(1, 1001) if worth(r, p, ratio, idle)), default=0) for r in POINTS}


PRICE_LIMIT = price_limits(None)
RELAXED_LIMIT = price_limits(RELAXED_RATIO, idle=True)
FLOOR_LIMIT = price_limits(FLOOR_RATIO, idle=True)
TIER_LIMITS = [(b, price_limits(r, idle=True)) for b, r in IDLE_TIERS]


def surplus_limit(rarity, budget):
    """Prix maximum des achats « surplus » pour cette rareté (0 si le solde ne dépasse pas la réserve)."""
    if rarity not in SURPLUS_RARITIES or budget <= SURPLUS_RESERVE:
        return 0
    return POINTS[rarity] * (SURPLUS_EPIC_COINS_PER_PT if rarity == "epic" else SURPLUS_COINS_PER_PT)


def tier_limit(rarity, budget):
    """Prix maximum accepté pour cette rareté avec ce solde, tous paliers confondus."""
    return max([lim[rarity] for b, lim in TIER_LIMITS if budget >= b], default=0)

# --- achat-revente : annonces sous la valeur de recyclage -------------------------
RECYCLE_VALUE = {"common": 5, "rare": 15, "epic": 40}
FLIP_MIN_PROFIT = {"common": 2, "rare": 3, "epic": 5}
FLIP_LIMIT = {r: RECYCLE_VALUE[r] - FLIP_MIN_PROFIT[r] for r in RECYCLE_VALUE}
MAX_FLIPS = 20  # garde-fou par exécution

# --- ventes de doublons ----------------------------------------------------------
SELL_RARITIES = set() if FEEDER else {"RARE", "EPIC"}  # Joss ne met rien en vente : il recycle
# (prix minimum, prix maximum, prix par défaut) en essecoins ; le recyclage rapporte 15 / 40
SELL_BOUNDS = {"RARE": (20, 30, 26), "EPIC": (50, 62, 60)}
SELL_TIMEOUT_MIN = int(os.environ.get("SELL_TIMEOUT_MIN", "120"))
STATE_FILE = os.environ.get("SELL_STATE_FILE", "sell_state.json")

MAX_RECYCLES = 100  # garde-fous par exécution
MAX_MORE_CLICKS = 60  # garde-fou : le marché des rares compte plusieurs centaines d'annonces à 15-20

report = []  # (section, message)
tables = {}  # section -> {"header": [...], "rows": [[...]]}
state = {"halt": False}  # passe à True si aucune action n'apparaît dans l'historique
pending = []  # actions cliquées, à vérifier dans l'historique


ALERT_MARKS = ("⚠", "Session expirée", "erreur ignorée", "arrêt du marché")


def log(msg, sec="Autres"):
    """Ligne de journal rangée dans une section du résumé (les alertes remontent en tête)."""
    print(msg)
    report.append(("À regarder" if any(k in msg for k in ALERT_MARKS) else sec, msg))


def table_row(sec, header, row):
    t = tables.setdefault(sec, {"header": header, "rows": []})
    t["rows"].append(row)


SECTIONS = [  # ordre d'affichage dans le résumé
    ("À regarder", "⚠ À regarder"),
    ("Joss", "Compte Joss (échange, appât, réserve)"),
    ("Amis", "Échanges avec les amis"),
    ("Doublons", "Doublons de ce passage"),
    ("Achats", "Achats"),
    ("Annonces", "Mes annonces en cours"),
    ("Vérifications", "Vérifications"),
    ("Lecture", "Lecture du marché"),
    ("Autres", "Autres"),
]


def write_summary(title):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    mode = "SIMULATION (rien n'a été cliqué)" if DRY_RUN else "ACTIONS RÉELLES"
    sell = "vente simulée" if SELL_DRY_RUN else "vente réelle"
    out = [f"## {title} : {mode} ({sell})", ""]
    bilan = [msg for sec, msg in report if sec == "Bilan"]
    out += [f"**{b}**  " for b in bilan] + [""] if bilan else []
    shown = False
    for key, label in SECTIONS:
        lines = [msg for sec, msg in report if sec == key]
        t = tables.get(key)
        if not lines and not t:
            continue
        shown = True
        n = f" ({len(t['rows'])})" if t else ""
        out.append(f"### {label}{n}")
        out += [f"- {l.strip()}" for l in lines]
        if t:
            out.append("| " + " | ".join(t["header"]) + " |")
            out.append("|" + "---|" * len(t["header"]))
            out += ["| " + " | ".join(str(c) for c in r) + " |" for r in t["rows"]]
        out.append("")
    if not shown and not bilan:
        out.append("Rien à faire.")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


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
        "owned": next((int(m.group(1)) for l in lines
                       if (m := re.fullmatch(r"you have ×(\d+)", l, re.I))), 0),
        "price": listing_price(lines),
    }


# ------------------------------------------------------------------ pages
def goto(page, path, tries=2):
    """Ouvre une page. Le site est parfois lent : on n'exige plus « networkidle » (attendu
    10 s au plus) et on réessaie une fois si le chargement échoue."""
    for attempt in range(tries):
        try:
            page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
            break
        except PlaywrightError:
            if attempt == tries - 1:
                raise
            print(f"Chargement de {path} échoué, nouvel essai.")
            page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightError:
        pass
    if "/login" in page.url:
        log("Session expirée : relance login_local.py et mets à jour le secret.")
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


# ------------------------------------------------- historique des essecoins
def history_text(page):
    goto(page, "/market/mine")
    return page.locator("main").inner_text()


def amounts_in(text, kind, name):
    """Montants (signés) des lignes « <kind> · <name> » de l'historique, plus récent en haut."""
    pat = rf"{re.escape(kind)} · {re.escape(name)}\s*\n\s*\d{{1,2}}/\d{{1,2}}/\d{{4}}\s*\n\s*([+\-−–]\s*\d+)"
    out = []
    for m in re.findall(pat, text):
        out.append(int(re.sub(r"[^\d]", "", m)) * (-1 if m.strip()[0] in "-−–" else 1))
    return out


def add_pending(kind, name, snapshot):
    """Action cliquée, à retrouver dans l'historique (comparé à l'instantané pris au départ)."""
    n_before = len(amounts_in(snapshot, kind, name)) if snapshot else 0
    pending.append({"kind": kind, "name": name, "n_before": n_before,
                    "confirmed": False, "t": time.time()})


def check_pending(page):
    """Relit l'historique une fois et marque les actions devenues visibles."""
    text = history_text(page)
    for p in pending:
        if not p["confirmed"] and len(amounts_in(text, p["kind"], p["name"])) > p["n_before"]:
            p["confirmed"] = True
    return sum(p["confirmed"] for p in pending)


def guard(page):
    """Le site affiche l'historique avec du retard. Si, longtemps après plusieurs actions,
    aucune n'y est visible, on arrête (les clics semblent sans effet)."""
    waiting = [p for p in pending if not p["confirmed"]]
    if len(waiting) >= 4 and time.time() - waiting[0]["t"] > 90 and check_pending(page) == 0:
        log("Aucune des dernières actions n'apparaît dans l'historique : arrêt du marché.", "À regarder")
        state["halt"] = True


# ---------------------------------------------- mémoire des annonces créées
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("listings"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"listings": []}


def save_state(data):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    set_output("state_changed", "true")


# ------------------------------------------------------ annonces en cours
ON_SALE_JS = r"""() => [...document.querySelectorAll("main form")]
  .filter(f => f.querySelector("input[name='listingId']")
    && [...f.querySelectorAll('button')].some(b => b.innerText.trim() === 'Withdraw'))
  .map(f => {
    let el = f;
    while (el.parentElement && el.parentElement.tagName !== 'MAIN'
           && el.parentElement.querySelectorAll("input[name='listingId']").length === 1) {
      el = el.parentElement;
    }
    return {lid: f.querySelector("input[name='listingId']").value, text: el.innerText};
  })"""


def on_sale(page):
    """Annonces à prix fixe de /market/mine (la page doit déjà être chargée)."""
    out = []
    for it in page.evaluate(ON_SALE_JS):
        if "fixed price" not in it["text"].lower():
            continue
        info = parse_listing(it["text"])
        if info and info["name"]:
            info["lid"] = it["lid"]
            out.append(info)
    return out


def withdraw(page, lid):
    press(page.locator(f"main form:has(input[name='listingId'][value='{lid}'])")
          .get_by_role("button", name="Withdraw", exact=True))
    settle(page)


def manage_listings(page):
    """Retire (puis laisse recycler) les annonces créées par le script qui n'ont pas été
    vendues dans le délai. Renvoie l'ensemble des noms retirés."""
    data = load_state()
    withdrawn = set()
    if not data["listings"]:
        return withdrawn
    goto(page, "/market/mine")
    current = on_sale(page)
    keep, now = [], time.time()
    for e in data["listings"]:
        cur = next((c for c in current if c["name"] == e["name"]), None)
        if cur is None:
            log(f"Annonce « {e['name']} » terminée (vendue ou retirée).", "Annonces")
            continue
        age = int((now - e["listed_at"]) / 60)
        if age < SELL_TIMEOUT_MIN:
            print(f"Annonce « {e['name']} » à {cur['price']} essecoins : en vente depuis {age} min.")
            table_row("Annonces", ["Carte", "Prix", "En vente depuis", "Retrait auto dans"],
                      [f"« {e['name']} »" + (" (appât Joss)" if e.get("bait") else ""),
                       f"{cur['price']}", f"{age} min", f"{max(SELL_TIMEOUT_MIN - age, 0)} min"])
            keep.append(e)
        elif SELL_DRY_RUN:
            log(f"[SIMULATION] Retirer l'annonce « {e['name']} » ({age} min sans vente) puis recycler.", "Annonces")
            keep.append(e)
        else:
            withdraw(page, cur["lid"])
            withdrawn.add(e["name"])
            log(f"Annonce « {e['name']} » retirée ({age} min sans vente) : la carte sera recyclée.", "Annonces")
    if len(keep) != len(data["listings"]) or withdrawn:
        data["listings"] = keep
        save_state(data)
    return withdrawn


# ----------------------------------------------------- doublons : vendre / recycler
SELL_TILES_JS = r"""() => [...document.querySelectorAll('main div.grid > div')].map(t => {
  const rb = [...t.querySelectorAll('button')].find(b => /^Recycle \d+/.test(b.innerText.trim()));
  return {text: t.innerText, recycle: rb ? rb.innerText.replace(/\s+/g, ' ').trim() : null};
})"""


def scan_sell(page, recyclable=RECYCLABLE):
    """Cartes avec un bouton « Recycle N » individuel (un seul appel au navigateur)."""
    out = []
    for i, t in enumerate(page.evaluate(SELL_TILES_JS)):
        label = t["recycle"]
        if not label or "duplicates" in label:
            continue  # jamais le bouton général
        lines = tile_lines(t["text"])
        rarity = tile_rarity(lines)
        if rarity not in recyclable:
            continue
        text = t["text"]
        m_copies = re.search(r"×\s*(\d+)", text)
        m_last = re.search(r"last sold\s+(\d+)", text, re.I)
        m_cheap = re.search(r"cheapest now\s+(\d+)", text, re.I)
        m_gain = re.search(r"\+\s*(\d+)", label)
        out.append({
            "idx": i, "rarity": rarity, "name": tile_name(lines, rarity), "label": label,
            "copies": int(m_copies.group(1)) if m_copies else 2,
            "gain": int(m_gain.group(1)) if m_gain else 0,
            "on_market": "on the market or in a trade" in text.lower(),
            "last_sold": int(m_last.group(1)) if m_last else None,
            "cheapest": int(m_cheap.group(1)) if m_cheap else None,
        })
    return out


def sell_price(it):
    lo, hi, default = SELL_BOUNDS[it["rarity"]]
    if it["rarity"] == "EPIC":
        # épique : on se cale sur le dernier prix de vente (il a trouvé preneur), un cran
        # sous l'annonce la moins chère si elle est plus basse ; plafond 62
        refs = [it["last_sold"]] if it["last_sold"] else []
        if it["cheapest"]:
            refs.append(it["cheapest"] - 1)
        return max(lo, min(hi, min(refs))) if refs else default
    ref = it["cheapest"] or it["last_sold"]
    if not ref:
        return default
    return max(lo, min(hi, ref - 1))  # un cran sous l'annonce la moins chère


def list_for_sale(page, it, price):
    tile = page.locator("main div.grid > div").nth(it["idx"])
    tile.locator("input[name='kind'][value='fixed']").check()
    tile.locator("input[name='price']").fill(str(price))
    press(tile.get_by_role("button", name="Put up for sale", exact=True))
    settle(page)


def recycle_everything(page, start):
    """Compte Joss : un seul clic sur « Recycle N duplicates » (toutes raretés, légendaires et
    Créateur compris ; le site garde un exemplaire de chaque carte). Jamais utilisé sur le principal."""
    items = scan_sell(page, RECYCLABLE_JOSS)
    btn = page.get_by_role("button", name=re.compile(r"^Recycle \d+ duplicates"))
    out = {"start": start, "gain": 0, "recycled": 0, "listed": 0}
    if not items or btn.count() == 0:
        return out
    m_gain = re.search(r"\+\s*(\d+)", btn.first.inner_text())
    gain = int(m_gain.group(1)) if m_gain else sum(it["gain"] for it in items)
    names = ", ".join(f"{it['rarity']} « {it['name']} »" for it in items)
    if DRY_RUN:
        log(f"[SIMULATION] Recycler tous les doublons ({len(items)} carte(s), ≈ +{gain} essecoins) : {names}",
            "Doublons")
        out.update(gain=gain)
        return out
    press(btn.first)
    settle(page)
    guard(page)
    left = items
    for _ in range(4):  # le site met un moment à se mettre à jour
        goto(page, "/market/sell")
        left = scan_sell(page, RECYCLABLE_JOSS)
        if not left:
            break
        page.wait_for_timeout(10000)
    log(f"Tous les doublons recyclés ({len(items)} carte(s), ≈ +{gain} essecoins) : {names}", "Doublons")
    if left:
        log(f"Encore {len(left)} carte(s) avec un bouton « Recycle » 30 s après le clic "
            "(retard du site probable, elles seront reprises au prochain passage).", "Doublons")
    out.update(gain=gain, recycled=len(items))
    return out


def recycle_all(page, snapshot, no_sell, keep_spare=0, keep_names=()):
    """Vend (RARE / EPIC) ou recycle les doublons."""
    goto(page, "/market/sell")
    start = balance(page)
    if FEEDER:
        return recycle_everything(page, start)
    handled = set()
    gain_total = 0
    recycled = listed = 0
    data = load_state()
    reserve_logged = False
    for _ in range(MAX_RECYCLES):
        if state["halt"]:
            break
        items = [it for it in scan_sell(page)
                 if it["name"] and it["name"] not in handled and not it["on_market"]
                 and it["name"] not in keep_names]  # appât / échange en cours : on n'y touche pas
        spare = sorted((it for it in items if it["rarity"] in ("COMMON", "RARE")),
                       key=lambda it: it["rarity"] != "COMMON")[:keep_spare]
        keep = {it["name"] for it in spare}
        if keep_spare and not reserve_logged:
            reserve_logged = True
            if spare:
                log("Gardés en réserve pour Joss (non recyclés) : "
                    + ", ".join(f"{it['rarity']} « {it['name']} »" for it in spare), "Joss")
            else:
                log("Réserve pour Joss demandée mais aucun doublon commun/rare disponible.", "Joss")
        items = [it for it in items if it["name"] not in keep]
        if not items:
            break
        if DRY_RUN:  # simulation complète : on liste tout, sans cliquer
            for it in items:
                if it["rarity"] in SELL_RARITIES and it["name"] not in no_sell:
                    log(f"[SIMULATION] Mettre en vente {it['rarity']} « {it['name']} » à {sell_price(it)} "
                        f"(last sold {it['last_sold']} · cheapest now {it['cheapest']}), "
                        f"sinon recyclage après {SELL_TIMEOUT_MIN} min", "Doublons")
                else:
                    log(f"[SIMULATION] Recycler {it['rarity']} « {it['name']} » ({it['label']})", "Doublons")
                gain_total += it["gain"]
            break
        it = items[0]
        handled.add(it["name"])
        if it["rarity"] in SELL_RARITIES and it["name"] not in no_sell:
            price = sell_price(it)
            if SELL_DRY_RUN:
                log(f"[SIMULATION] Mettre en vente {it['rarity']} « {it['name']} » à {price} "
                    f"(last sold {it['last_sold']} · cheapest now {it['cheapest']}), "
                    f"sinon recyclage après {SELL_TIMEOUT_MIN} min. En attendant : recyclage.", "Doublons")
            else:
                list_for_sale(page, it, price)
                data["listings"].append({"name": it["name"], "price": price, "listed_at": time.time()})
                save_state(data)
                listed += 1
                log(f"Mis en vente {it['rarity']} « {it['name']} » à {price} essecoins "
                    f"(last sold {it['last_sold']} · cheapest now {it['cheapest']}), "
                    f"recyclage si invendu après {SELL_TIMEOUT_MIN} min.", "Doublons")
                goto(page, "/market/sell")
                continue
        press(page.locator("main div.grid > div").nth(it["idx"])
              .get_by_role("button", name=re.compile(r"^Recycle \d+")).first)
        settle(page)
        add_pending("Duplicate recycled", it["name"], snapshot)
        recycled += 1
        gain_total += it["gain"]
        log(f"Recyclage envoyé {it['rarity']} « {it['name']} » (≈ +{it['gain']} essecoins)", "Doublons")
        guard(page)
        goto(page, "/market/sell")
    return {"start": start, "gain": gain_total, "recycled": recycled, "listed": listed}


def verify_listings(page, names):
    """Contrôle que les cartes mises en vente apparaissent bien dans « My cards on sale ».
    Le site affiche cette liste avec du retard : on réessaie jusqu'à 3 fois (20 s d'écart)."""
    if not names:
        return
    missing = set(names)
    for attempt in range(4):
        goto(page, "/market/mine")
        missing -= {c["name"] for c in on_sale(page)}
        if not missing:
            break
        if attempt < 3:
            page.wait_for_timeout(20000)
    for n in names:
        if n in missing:
            log(f"  ⚠ Annonce « {n} » introuvable dans « My cards on sale » (même après 60 s).", "Vérifications")
    ok = len(names) - len(missing)
    if ok:
        log(f"{ok}/{len(names)} annonce(s) visible(s) dans « My cards on sale ».", "Vérifications")


# ------------------------------------------------------------------ achats
BUY_FORMS = "main form:has(input[name='listingId']):has(button:text-is('Buy'))"

LISTINGS_JS = r"""() => [...document.querySelectorAll("main form")]
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


def market_path(rarity, q=None):
    path = f"/market?rarity={rarity}&kind=fixed&sort=price"
    return path + f"&q={quote(q)}" if q else path


def show_more(page, more):
    """Clique « Show more listings » et attend que de nouvelles annonces s'affichent."""
    before = page.locator(BUY_FORMS).count()
    press(more.first)
    try:
        page.wait_for_function(
            "([sel, n]) => document.querySelectorAll(sel).length > n", arg=[BUY_FORMS, before],
            timeout=8000)
    except Exception:  # noqa: BLE001  (plus d'annonce à charger, ou chargement lent)
        page.wait_for_timeout(1000)


PAGE_SIZE = 24  # le site charge les annonces 24 par 24


def fetch_listings(page, rarity, limit):
    """Annonces à prix fixe de cette rareté, des moins chères jusqu'au prix `limit`.
    Le bouton « Show more listings » disparaît un instant pendant le chargement : on ne
    conclut à la fin de la liste que s'il reste absent et que le nombre d'annonces n'est
    pas un multiple de 24 (sinon : attente, puis un seul rechargement)."""
    path = market_path(rarity)
    goto(page, path)
    try:
        page.wait_for_selector(BUY_FORMS, timeout=5000)
    except PlaywrightError:
        pass  # aucune annonce (ou chargement lent : on réessaiera plus bas)
    more_clicks = reloads = 0
    while True:
        listings = collect_listings(page)
        last_price = listings[-1]["price"] if listings else None
        if more_clicks >= MAX_MORE_CLICKS or (last_price is not None and last_price > limit):
            break
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            n = len(listings)
            if n and n % PAGE_SIZE != 0:
                break  # nombre « rond » : vraie fin de la liste, inutile d'attendre
            try:
                more.first.wait_for(state="visible", timeout=4000)
            except PlaywrightError:
                pass
            if more.count() == 0:
                if len(collect_listings(page)) > n:
                    continue  # des annonces sont arrivées entre-temps
                if reloads < 1:
                    reloads += 1
                    print(f"Marché {rarity} : liste coupée à {n} annonce(s), rechargement.")
                    goto(page, path)
                    more_clicks = 0
                    continue
                break  # fin de la liste (ou site trop lent : on garde ce qui est lu)
        show_more(page, more)
        more_clicks += 1
    return [l for l in listings if l["rarity"].lower() == rarity and l["price"] <= limit]


def market_scan(page, budget=0):
    """Une lecture du marché par rareté (sert à l'achat-revente et aux achats)."""
    out = {}
    for rarity in POINTS:
        limit = max(RELAXED_LIMIT[rarity], PRICE_LIMIT[rarity], FLIP_LIMIT.get(rarity, 0))
        limit = max(limit, tier_limit(rarity, budget), surplus_limit(rarity, budget))
        out[rarity] = fetch_listings(page, rarity, limit) if limit > 0 else []
        got = out[rarity]
        new = [l for l in got if l["is_new"]]
        print(f"Marché {rarity} : {len(got)} annonce(s) lues jusqu'à {limit} essecoins, "
              f"{len(new)} carte(s) manquante(s)"
              + (f" (dès {min(l['price'] for l in new)})" if new else ""))
        table_row("Lecture", ["Rareté", "Annonces lues", "Jusqu'à (essecoins)", "Cartes qui me manquent",
                              "La moins chère"],
                  [rarity, len(got), limit, len(new), min(l["price"] for l in new) if new else "-"])
    return out


def cheapest_by_name(listings):
    best = {}
    for l in listings:
        if l["name"] and (l["name"] not in best or l["price"] < best[l["name"]]["price"]):
            best[l["name"]] = l
    return list(best.values())


def locate_listing(page, rarity, name, limit, owned=False):
    """Recharge la liste et retrouve l'annonce de cette carte (nouvelle pour nous, ou
    déjà possédée pour l'achat-revente), au prix `limit` au plus."""
    goto(page, market_path(rarity, name))  # recherche par nom : peu d'annonces à parcourir
    for _ in range(6):
        cur = [x for x in collect_listings(page)
               if x["name"] == name and x["price"] <= limit
               and ((x["owned"] >= 1 and not x["is_new"]) if owned else x["is_new"])]
        if cur:
            return cur[0]
        more = page.get_by_role("button", name="Show more listings")
        if more.count() == 0:
            return None
        show_more(page, more)
    return None


def do_buy(page, rarity, name, price, snapshot, owned=False):
    """Achète l'annonce (au prix vu ou moins). Renvoie le prix, ou None si disparue."""
    found = locate_listing(page, rarity, name, price, owned)
    if found is None:
        return None
    press(page.locator(BUY_FORMS).nth(found["idx"]).get_by_role("button", name="Buy", exact=True))
    settle(page)
    add_pending("Card bought", name, snapshot)
    guard(page)
    return found["price"]


def flip_all(page, scan, budget, snapshot):
    """Achat-revente : cartes déjà possédées, annoncées bien sous leur valeur de recyclage."""
    cands = []
    for rarity in RECYCLE_VALUE:
        cands += [dict(l, profit=RECYCLE_VALUE[rarity] - l["price"])
                  for l in cheapest_by_name(scan.get(rarity, []))
                  if not l["is_new"] and l["owned"] >= 1 and l["price"] <= FLIP_LIMIT[rarity]]
    cands.sort(key=lambda c: (-c["profit"], c["price"]))
    flipped = set()
    for c in cands:
        if state["halt"] or len(flipped) >= MAX_FLIPS:
            break
        if c["price"] > budget:
            continue
        txt = (f"{c['rarity']} « {c['name']} » à {c['price']} essecoins "
               f"(recyclage +{RECYCLE_VALUE[c['rarity'].lower()]}, gain ≈ +{c['profit']})")
        if DRY_RUN:
            log(f"[SIMULATION] Achat-revente {txt}", "Achats")
            budget -= c["price"]
            flipped.add(c["name"])
            continue
        paid = do_buy(page, c["rarity"].lower(), c["name"], c["price"], snapshot, owned=True)
        if paid is None:
            continue  # déjà vendue
        budget -= paid
        flipped.add(c["name"])
        log(f"Achat-revente envoyé {txt}", "Achats")
    return budget, flipped


def buy_all(page, scan, budget, snapshot, skip=()):
    bought = set(skip)
    purchases = 0
    cands = []
    for rarity in POINTS:
        cands += cheapest_by_name([l for l in scan.get(rarity, []) if l["is_new"]])

    def key(c):
        pts = POINTS[c["rarity"].lower()]
        return (-pts / c["price"], -pts, c["price"])

    def buy(c, tag=""):
        nonlocal budget, purchases
        rarity, pts = c["rarity"].lower(), POINTS[c["rarity"].lower()]
        bought.add(c["name"])
        if DRY_RUN:
            paid = c["price"]
            log(f"[SIMULATION] Acheter {c['rarity']} « {c['name']} » à {paid} essecoins "
                f"({pts} pt, {pts / paid:.2f} pt/essecoin){tag}", "Achats")
        else:
            paid = do_buy(page, rarity, c["name"], c["price"], snapshot)
            if paid is None:
                return  # plus disponible (vendue ou déjà possédée)
            log(f"Achat envoyé {c['rarity']} « {c['name']} » à {paid} essecoins "
                f"({pts} pt, {pts / paid:.2f} pt/essecoin){tag}", "Achats")
        budget -= paid
        purchases += 1

    # passe 1 : ratio normal, meilleurs points par essecoin d'abord
    held = False
    p1 = sorted([c for c in cands if worth(c["rarity"], c["price"])], key=key)
    for c in p1:
        if state["halt"] or c["name"] in bought:
            continue
        if c["price"] > budget:
            held = True
            log(f"Mise de côté pour {c['rarity']} « {c['name']} » à {c['price']} essecoins "
                f"(solde {budget}) : pas d'achat moins rentable en attendant.", "Achats")
            break
        buy(c)

    # passe 2 : ratio assoupli (épique <= 100, légendaire <= 250) pour ne pas stagner. On attend
    # seulement si une meilleure carte est à moins de HOLD_AHEAD essecoins du solde.
    def relaxed_pass(ratio, label):
        nonlocal budget
        pool = sorted([c for c in cands if c["name"] not in bought
                       and worth(c["rarity"], c["price"], ratio, idle=True)], key=key)
        for c in pool:
            if state["halt"]:
                break
            if c["price"] > budget or budget - c["price"] < RESERVE:
                if 0 < c["price"] - budget <= HOLD_AHEAD:
                    log(f"Mise de côté ({label}) pour {c['rarity']} « {c['name']} » à "
                        f"{c['price']} essecoins (solde {budget}).", "Achats")
                    break
                continue
            buy(c)

    if not held and budget >= IDLE_BALANCE:
        relaxed_pass(RELAXED_RATIO, "ratio assoupli")
        # passe 3 : le solde s'accumule malgré tout -> paliers progressifs (épiques / légendaires)
        for tier_balance, tier_ratio in IDLE_TIERS:
            if budget >= tier_balance:
                relaxed_pass(tier_ratio, f"palier {tier_balance}+ : ratio {tier_ratio}")
        # passe 4 : surplus au-dessus de la réserve -> communes, rares (100/pt) et épiques (80/pt)
        surplus = sorted([c for c in cands if c["name"] not in bought
                          and c["price"] <= surplus_limit(c["rarity"].lower(), budget)], key=key)
        done = 0
        for c in surplus:
            if state["halt"] or done >= MAX_SURPLUS_BUYS:
                break
            if budget - c["price"] < SURPLUS_RESERVE:
                continue  # la réserve n'est jamais entamée
            buy(c, f" [surplus : réserve {SURPLUS_RESERVE} gardée]")
            done += 1
    return budget, purchases


def friend_trades(page, engaged):
    """Échanges avec les amis (friends.py) : doublons à ne pas vendre ni recycler. Ne bloque jamais le marché."""
    try:
        import friends
        if not friends.FRIEND_NAMES:
            return set()
        data = load_state()
        before = json.dumps(data, sort_keys=True)
        keep = friends.friend_step(page, data, engaged)
        if json.dumps(data, sort_keys=True) != before:
            save_state(data)
        return keep
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"Échanges amis : erreur ignorée ({type(exc).__name__}: {str(exc)[:120]}).", "Amis")
        return set()


def remember_market_missing(scan):
    """Cartes qui me manquent et qu'on trouve déjà au marché à un prix que le surplus accepte :
    inutile de les demander à un ami (elles s'achètent avec des essecoins)."""
    try:
        names = sorted({l["name"] for r in ("common", "rare") for l in scan.get(r, [])
                        if l["is_new"] and l["name"] and l["price"] <= surplus_limit(r, 10 ** 6)})
        data = load_state()
        if data.get("market_missing") != names:
            data["market_missing"] = names
            save_state(data)
    except Exception as exc:  # noqa: BLE001
        print(f"market_missing non enregistré ({type(exc).__name__}).")


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    session = json.loads(raw)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=session, locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.on("dialog", lambda d: d.accept())  # confirmations éventuelles

        # instantané de l'historique avant d'agir : sert de référence à la vérification finale
        snapshot = None if DRY_RUN else history_text(page)

        withdrawn = manage_listings(page)
        keep_spare, keep_names = 0, set()
        if not FEEDER:
            import link  # compte nourrisseur : échange + annonce appât, avant le recyclage des doublons
            keep_spare, keep_names = link.main_link(page)  # réserve pour Joss + cartes déjà engagées
            keep_names = set(keep_names) | friend_trades(page, keep_names)  # amis : propositions et réserve
        r1 = recycle_all(page, snapshot, withdrawn, keep_spare, keep_names)
        start, recycled, listed = r1["start"], r1["recycled"], r1["listed"]
        # Budget = solde déjà crédité. Les recyclages de ce passage ne sont pas comptés :
        # le site les crédite avec du retard et un achat serait refusé faute de solde
        # (ils serviront au passage suivant, 5 minutes plus tard).
        budget = start or 0
        if DRY_RUN:
            budget += r1["gain"]  # simulation : on montre ce que ferait le passage complet

        purchases = flips = 0
        if budget > 0 and not FEEDER:
            scan = market_scan(page, budget)
            remember_market_missing(scan)
            # Les cartes de l'achat-revente suivent ensuite le circuit normal des doublons
            # (le site n'affiche pas tout de suite la carte achetée) : recyclage ou mise en vente.
            budget, flipped = flip_all(page, scan, budget, snapshot)
            flips = len(flipped)
            budget, purchases = buy_all(page, scan, budget, snapshot, skip=flipped)

        if listed:
            verify_listings(page, [e["name"] for e in load_state()["listings"]])

        if pending:  # vérification finale : l'historique peut avoir du retard
            ok = 0
            for attempt in range(4):
                ok = check_pending(page)
                if ok == len(pending):
                    break
                if attempt < 3:
                    page.wait_for_timeout(20000)
            log(f"Historique du site : {ok}/{len(pending)} action(s) retrouvée(s).", "Vérifications")
            for p_ in pending:
                if not p_["confirmed"]:
                    log(f"  ⚠ « {p_['name']} » ({p_['kind']}) toujours absent de l'historique.", "Vérifications")
        goto(page, "/market")
        final = balance(page)
        log(f"Solde : {start} → {final} essecoins · {recycled} recyclage(s) · {listed} mise(s) en vente · "
            f"{flips} achat(s)-revente · {purchases} achat(s)", "Bilan")
        browser.close()
    write_summary("Marché (compte Joss)" if FEEDER else "Marché")


if __name__ == "__main__":
    main()
