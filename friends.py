"""Échanges automatiques avec des amis (hors Joss) : mes doublons contre les cartes qui me manquent.

Appelé par market.py (compte principal) avant le recyclage des doublons : `friend_step(page)`.

Règles :
* Seuls les amis de FRIEND_TRADE_NAMES (séparés par « | ») reçoivent des propositions. Joss n'en
  reçoit jamais d'ici (son circuit reste celui de link.py).
* On propose à un ami des cartes qu'il ne possède pas, prises parmi mes doublons (jamais
  légendaire sauf exception ci-dessous, ni Créateur), contre des cartes qu'il a et qui me manquent (« New for you »),
  à valeur égale en points (commune 1, rare 3, épique 8) : je peux surpayer d'au plus
  FRIEND_TOLERANCE point. On vise d'abord les cartes les plus rares, et on laisse de côté celles
  qu'on trouve déjà au marché à un prix que le surplus accepte (elles s'achètent avec des essecoins).
* Amis prioritaires (FRIEND_PRIORITY_NAMES : Mateo, Baptiste) : traités en premier, donc ils prennent les doublons
  avant les autres, et je peux leur offrir un échange à leur avantage jusqu'à FRIEND_PRIORITY_OVERPAY points (15).
* Les doublons légendaires ne sont proposés qu'aux amis de FRIEND_LEGENDARY_NAMES (Mateo, Baptiste).
* Une carte déjà demandée dans une proposition en attente n'est pas redemandée à un autre ami.
* Une seule proposition à la fois par ami, au plus 5 cartes de chaque côté.
* Sans réponse après FRIEND_TRADE_EXPIRY_MIN minutes (120), la proposition est annulée : les
  cartes reviennent au circuit normal (vente ou recyclage). L'ami est ensuite laissé tranquille
  FRIEND_COOLDOWN_MIN minutes (360). Un échange refusé suit la même pause.
* Les doublons utiles aux échanges ne sont ni vendus ni recyclés (réserve plafonnée à la valeur
  des cartes que les amis détiennent encore et qui me manquent).
Simulation tant que FRIEND_TRADE_DRY_RUN n'est pas « false ».
"""
import itertools
import os
import re
import time

import market as M
import link

FRIEND_DRY_RUN = M.flag("FRIEND_TRADE_DRY_RUN") or M.DRY_RUN
FRIEND_NAMES = [n.strip() for n in os.environ.get("FRIEND_TRADE_NAMES", "").split("|") if n.strip()]
EXPIRY_MIN = int(os.environ.get("FRIEND_TRADE_EXPIRY_MIN", "120"))
COOLDOWN_MIN = int(os.environ.get("FRIEND_COOLDOWN_MIN", "360"))
REFRESH_MIN = int(os.environ.get("FRIEND_REFRESH_MIN", "20"))   # relecture de la collection d'un ami
TOLERANCE = int(os.environ.get("FRIEND_TOLERANCE", "1"))        # surpaiement maximal, en points
MAX_SIDE = 5
GIVE_RARITIES = ("COMMON", "RARE", "EPIC")  # jamais Créateur ; légendaire seulement pour LEGENDARY_NAMES
# amis à qui l'on peut donner un doublon légendaire (les autres n'en reçoivent jamais)
LEGENDARY_NAMES = [n.strip().lower() for n in
                   os.environ.get("FRIEND_LEGENDARY_NAMES", "Mateo BOURREL|Baptiste Brocard").split("|") if n.strip()]
PTS = {"COMMON": 1, "RARE": 3, "EPIC": 8, "LEGENDARY": 20}
# Amis prioritaires (Mateo, Baptiste) : servis en premier, et je peux leur offrir un échange à leur avantage
# (surplus accepté, en points) ; les autres amis restent à FRIEND_TOLERANCE.
PRIORITY_NAMES = [n.strip().lower() for n in
                  os.environ.get("FRIEND_PRIORITY_NAMES", "Mateo BOURREL|Baptiste Brocard").split("|") if n.strip()]
PRIORITY_OVERPAY = int(os.environ.get("FRIEND_PRIORITY_OVERPAY", "15"))

CLOSED = ("declined", "refused", "cancel", "expired", "withdrawn")


def A(msg):
    """Journal du volet amis (section « Amis » du résumé)."""
    M.log(msg, "Amis")


def parse_collection(text):
    """Noms des cartes d'une collection d'ami (page /friends/<id>)."""
    lines = M.tile_lines(text)
    names = set()
    for i, l in enumerate(lines):
        if l.upper() in M.RARITIES:
            name = M.tile_name(lines[i:], l.upper())
            if name:
                names.add(name)
    return names


def friend_id(href):
    m = re.search(r"friend=([\w-]+)", href or "")
    return m.group(1) if m else None


def read_friend_collection(page, fid):
    M.goto(page, f"/friends/{fid}")
    page.wait_for_timeout(1500)
    m = re.search(r"(\d+)\s*/\s*\d+\s*cards", page.locator("main").inner_text())
    expected = int(m.group(1)) if m else 0
    for _ in range(30):
        names = parse_collection(page.locator("main").inner_text())
        if expected and len(names) >= expected - 1:
            break
        more = page.get_by_role("button", name=re.compile("Show more", re.I))
        if more.count() == 0:
            break
        M.press(more.first)
        page.wait_for_timeout(1000)
    return names


def spare_dups(page, reserved):
    """Mes doublons échangeables (nom -> {rarity, copies}) : jamais Créateur (les légendaires sont filtrés par ami),
    jamais une carte déjà en vente ou en échange, jamais celles de `reserved`."""
    M.goto(page, "/market/sell")
    out = {}
    for it in M.scan_sell(page, recyclable=M.RECYCLABLE | {"LEGENDARY"}):
        if it["rarity"] in GIVE_RARITIES + ("LEGENDARY",) and it["name"] and not it["on_market"] \
                and it["name"] not in reserved:
            out[it["name"]] = {"rarity": it["rarity"], "copies": it.get("copies", 2)}
    return out


def best_swap(want, give, tolerance=TOLERANCE):
    """Meilleur échange à valeur (presque) égale.
    want : [(nom, points)] cartes que je veux ; give : [(nom, points)] doublons que je peux donner.
    Renvoie (get, give) ou None. On maximise les points reçus, puis on minimise le surpaiement
    et le nombre de cartes données."""
    want = sorted(want, key=lambda c: (-c[1], c[0]))[:10]
    give = sorted(give, key=lambda c: (c[1], c[0]))
    if len(give) > 10:
        give = give[:6] + give[-4:]  # les moins chers et les plus précieux (légendaires comprises)

    def subsets(items):
        for k in range(1, min(MAX_SIDE, len(items)) + 1):
            for combo in itertools.combinations(items, k):
                yield combo, sum(c[1] for c in combo)

    gets = {}
    for combo, v in subsets(want):
        best = gets.get(v)
        if best is None or len(combo) < len(best):
            gets[v] = combo
    best_key, best_pair = None, None
    for dcombo, dv in subsets(give):
        for gv, gcombo in gets.items():
            if gv <= dv <= gv + tolerance:
                key = (-gv, dv - gv, len(dcombo), len(gcombo))
                if best_key is None or key < best_key:
                    best_key, best_pair = key, (list(gcombo), list(dcombo))
    return best_pair


def pending_status(text, friend, gets):
    """'done' | 'closed' | 'pending' pour la proposition envoyée à `friend` (cartes reçues `gets`).
    Sur /market/trades : « Trades you proposed » → « Waiting for <ami> » (bouton Cancel) tant que l'ami
    n'a pas répondu, puis « Past trades » : « <ami> · +reçues / −données » suivi de Done / Declined /
    Cancelled (avec un décalage d'environ 1 à 2 minutes)."""
    status = link.past_trade_status(text, friend, gets)
    if status is None:
        return "pending"
    s = status.lower()
    if s.startswith("done"):
        return "done"
    if any(k in s for k in CLOSED):
        return "closed"
    return "pending"


CANCEL_JS = r"""([friend, names]) => {
  const re = /^\s*cancel\s*$/i;
  const btns = [...document.querySelectorAll('main button')].filter(b => re.test(b.innerText));
  return btns.map(b => {
    let el = b;
    while (el.parentElement && el.parentElement.tagName !== 'MAIN'
           && [...el.parentElement.querySelectorAll('button')].filter(x => re.test(x.innerText)).length <= 1) {
      el = el.parentElement;
    }
    return el.innerText;
  });
}"""


def cancel_trade(page, friend, gets):
    """Annule la proposition en attente (bouton d'annulation du site). True si cliqué."""
    M.goto(page, "/market/trades")
    blocks = page.evaluate(CANCEL_JS, [friend, gets])
    for i, t in enumerate(blocks):
        if friend.lower() in t.lower() and all(n in t for n in gets):
            btn = page.locator("main button").filter(has_text=re.compile(r"^\s*cancel\s*$", re.I)).nth(i)
            M.press(btn)
            M.settle(page)
            return True
    A(f"⚠ Bouton d'annulation introuvable pour l'échange avec {friend} "
      f"({len(blocks)} bouton(s) d'annulation sur la page) : proposition laissée en place.")
    return False


def send_proposal(page, href, get_names, give_names):
    """Remplit et envoie la proposition. Renvoie True si envoyée."""
    M.goto(page, href)
    get = {c["name"]: c for c in link.trade_cards(page, "get")}
    give = {c["name"]: c for c in link.trade_cards(page, "give")}
    if any(n not in get for n in get_names) or any(n not in give for n in give_names):
        A("Échange ami : une carte n'apparaît pas dans le formulaire, abandon (rien envoyé).")
        return False
    for n in get_names:
        page.locator("main label:has(input[name='get'])").nth(get[n]["idx"]).click()
    for n in give_names:
        page.locator("main label:has(input[name='give'])").nth(give[n]["idx"]).click()
    got = page.locator("main input[name='get']:checked").count()
    gave = page.locator("main input[name='give']:checked").count()
    if got != len(get_names) or gave != len(give_names):
        A(f"Échange ami : sélection inattendue ({got}/{gave}), abandon (rien envoyé).")
        return False
    M.press(page.get_by_role("button", name="Send the proposal", exact=True))
    M.settle(page)
    return True


def friend_step(page, data, engaged=()):
    """Gère les propositions aux amis. Renvoie les noms de doublons à ne pas vendre ni recycler."""
    if not FRIEND_NAMES:
        return set()
    store = data.setdefault("friends", {})
    now = time.time()
    keep = set()
    buyable = set(data.get("market_missing", []))  # cartes manquantes déjà achetables au marché
    reserve_names = {}  # cartes que les amis détiennent encore et qui me manquent -> points (plafond de la réserve)
    friends_view = {}
    M.goto(page, "/market/trades")
    text = page.locator("main").inner_text()
    hrefs = {}
    for name in FRIEND_NAMES:
        if link.FEEDER_NAME.lower() in name.lower():
            continue  # Joss garde son circuit propre
        hrefs[name] = link.friend_href(page, name)

    # 1. suivi des propositions en cours
    for name, href in hrefs.items():
        st = store.setdefault(name, {})
        tr = st.get("trade")
        if not tr:
            continue
        age = int((now - tr["t"]) / 60)
        status = pending_status(text, name, tr["get"])
        label = f"{name} : +{', '.join(tr['get'])} / −{', '.join(tr['give'])}"
        if status == "done":
            A(f"Échange accepté par {label}.")
            st.pop("trade")
            st["next_at"] = now + 30 * 60
        elif status == "closed":
            A(f"Échange refusé / clos ({label}) : pause de {COOLDOWN_MIN} min avec cet ami.")
            st.pop("trade")
            st["next_at"] = now + COOLDOWN_MIN * 60
        elif age >= EXPIRY_MIN:
            if FRIEND_DRY_RUN or cancel_trade(page, name, tr["get"]):
                A(f"Échange sans réponse après {age} min ({label}) : annulé, "
                  f"les cartes retournent au circuit normal. Pause de {COOLDOWN_MIN} min.")
                st.pop("trade")
                st["next_at"] = now + COOLDOWN_MIN * 60
        else:
            A(f"Échange en attente avec {label} depuis {age} min (annulation à {EXPIRY_MIN} min).")
        if st.get("trade"):
            keep.update(tr["give"])

    # 2. relecture des amis, propositions
    # cartes déjà demandées dans une autre proposition en attente : inutile de les redemander à quelqu'un d'autre
    # (si deux amis acceptent, la seconde carte ne serait qu'un doublon)
    claimed = {n for v in store.values() if v.get("trade") for n in v["trade"].get("get", [])}
    dups = None
    # amis prioritaires d'abord : ils choisissent les doublons avant les autres
    order = sorted(hrefs.items(), key=lambda kv: (not any(l in kv[0].lower() for l in PRIORITY_NAMES), kv[0]))
    for name, href in order:
        st = store[name]
        refreshed = now - st.get("checked_at", 0) >= REFRESH_MIN * 60 or "wanted" not in st
        if refreshed:
            fid = friend_id(href)
            if not fid:
                continue
            try:
                has = read_friend_collection(page, fid)
                M.goto(page, href)
                wanted = [{"name": c["name"], "rarity": c["rarity"]}
                          for c in link.trade_cards(page, "get") if c["is_new"] and c["rarity"] in PTS]
            except Exception as exc:  # noqa: BLE001
                A(f"Ami {name} : lecture impossible ({type(exc).__name__}), on réessaiera.")
                continue
            st.update({"checked_at": now, "has": sorted(has), "wanted": wanted})
        wanted_pts = [(w["name"], PTS[w["rarity"]]) for w in st.get("wanted", [])]
        exclusive = [w for w in wanted_pts if w[0] not in buyable and w[0] not in claimed]
        reserve_names.update(dict(exclusive))
        friends_view[name] = st
        if st.get("trade") or now < st.get("next_at", 0):
            continue
        if not exclusive:
            if refreshed:
                A(f"Ami {name} : rien d'exclusif à demander ({len(wanted_pts)} carte(s) manquante(s), "
                  f"toutes déjà achetables au marché ou aucune).")
            continue
        if dups is None:
            dups = spare_dups(page, set(engaged) | keep)
        has = set(st.get("has", []))
        legendary_ok = any(l in name.lower() for l in LEGENDARY_NAMES)
        gives = [(n, PTS[d["rarity"]]) for n, d in dups.items() if n not in has and n not in keep
                 and (d["rarity"] != "LEGENDARY" or legendary_ok)]
        is_priority = any(l in name.lower() for l in PRIORITY_NAMES)
        pair = best_swap(exclusive, gives, PRIORITY_OVERPAY if is_priority else TOLERANCE)
        if not pair:
            if refreshed:
              A(f"Ami {name} : pas d'échange à valeur égale possible avec mes doublons "
                f"({len(exclusive)} carte(s) à demander, {len(gives)} doublon(s) à offrir).")
            continue
        get, give = pair
        gv, dv = sum(p for _, p in get), sum(p for _, p in give)
        txt = (f"Échange avec {name} : je donne {', '.join(n for n, _ in give)} ({dv} pts), "
               f"je reçois {', '.join(n for n, _ in get)} ({gv} pts)")
        if FRIEND_DRY_RUN:
            A(f"[SIMULATION] {txt}")
            claimed.update(n for n, _ in get)
            keep.update(n for n, _ in give)
            continue
        if send_proposal(page, href, [n for n, _ in get], [n for n, _ in give]):
            st["trade"] = {"t": now, "get": [n for n, _ in get], "give": [n for n, _ in give]}
            claimed.update(n for n, _ in get)
            keep.update(n for n, _ in give)
            A(f"{txt} : proposition envoyée.")

    # 3. réserve : doublons utiles aux amis, plafonnés à la valeur des cartes à obtenir
    reserve_wanted = sum(reserve_names.values())
    if reserve_wanted > 0 and friends_view:
        if dups is None:
            dups = spare_dups(page, set(engaged) | keep)
        useful = sorted(((n, d) for n, d in dups.items()
                         if n not in keep and d["rarity"] != "LEGENDARY"  # jamais vendus : pas de réserve à faire
                         and any(n not in set(v.get("has", [])) for v in friends_view.values())),
                        key=lambda x: PTS[x[1]["rarity"]])
        held, budget = [], reserve_wanted
        for n, d in useful:
            v = PTS[d["rarity"]]
            if v <= budget:
                held.append(n)
                keep.add(n)
                budget -= v
        if held:
            A("Doublons gardés pour les échanges avec les amis (ni vendus ni recyclés) : "
              + ", ".join(held))
    if FRIEND_DRY_RUN:
        return set()  # simulation : on ne retient aucun doublon, le circuit normal reste inchangé
    return keep
