"""Suivi du classement Essec Masters : points et cartes des 3 premiers (et de toi).

À chaque passage (au plus un instantané toutes les TRACK_EVERY_MIN minutes), lit
/leaderboard et ajoute des lignes « ts_utc,rank,name,cards,points,me » à un fichier CSV.
Dans le workflow, ce CSV vit sur la branche « tracking » (voir booster.yml).

  python leaderboard.py             instantané (écrit TRACK_CSV)
  python leaderboard.py --report    résumé de l'évolution à partir du CSV, sans navigateur

Variables : ESSEC_STORAGE_STATE (secret), TRACK_CSV (défaut leaderboard.csv),
TRACK_EVERY_MIN (défaut 30), TRACK_TOP (défaut 3).
"""
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

BASE = "https://www.essecmasters.com"
CSV_FILE = os.environ.get("TRACK_CSV", "leaderboard.csv")
EVERY_MIN = float(os.environ.get("TRACK_EVERY_MIN", "30"))
TOP = int(os.environ.get("TRACK_TOP", "3"))
FIELDS = ["ts_utc", "rank", "name", "cards", "points", "me"]


def parse_leaderboard(text):
    """Lignes du classement : [{rank, name, cards, points, me}] dans l'ordre de la page.

    Chaque entrée est « nom », « N different cards », « M pts » (précédés d'une médaille,
    d'un numéro de rang ou d'initiales qu'on ignore)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    rows = []
    for i, l in enumerate(lines):
        m = re.fullmatch(r"([\d\s.,]+) different cards?", l, re.I)
        if not m or i == 0 or i + 1 >= len(lines):
            continue
        p = re.fullmatch(r"([\d\s.,]+) pts?", lines[i + 1], re.I)
        if not p:
            continue
        name = lines[i - 1]
        me = bool(re.search(r"\(you\)\s*$", name, re.I))
        name = re.sub(r"\s*\(you\)\s*$", "", name, flags=re.I)
        rows.append({
            "rank": len(rows) + 1,
            "name": name,
            "cards": int(re.sub(r"\D", "", m.group(1))),
            "points": int(re.sub(r"\D", "", p.group(1))),
            "me": me,
        })
    return rows


def last_timestamp(path):
    if not os.path.exists(path):
        return None
    last = None
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last = row["ts_utc"]
    return datetime.fromisoformat(last) if last else None


def select(rows):
    """Les TOP premiers, plus toi si tu n'en fais pas partie."""
    keep = [r for r in rows if r["rank"] <= TOP]
    keep += [r for r in rows if r["me"] and r["rank"] > TOP]
    return keep


def append_rows(path, rows, now):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        for r in rows:
            w.writerow([now.isoformat(timespec="seconds"), r["rank"], r["name"],
                        r["cards"], r["points"], int(r["me"])])


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def snapshot():
    from playwright.sync_api import sync_playwright

    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    now = datetime.now(timezone.utc)
    prev = last_timestamp(CSV_FILE)
    if prev is not None and (now - prev).total_seconds() < EVERY_MIN * 60:
        print(f"Dernier instantané il y a {(now - prev).total_seconds() / 60:.0f} min : rien à faire.")
        set_output("changed", "false")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.goto(BASE + "/leaderboard", wait_until="networkidle")
        if "/login" in page.url:
            sys.exit("Session expirée : relance login_local.py et mets à jour le secret.")
        text = page.inner_text("main")
        browser.close()
    rows = parse_leaderboard(text)
    if len(rows) < TOP:
        sys.exit(f"Classement illisible ({len(rows)} ligne(s) trouvée(s)).")
    keep = select(rows)
    append_rows(CSV_FILE, keep, now)
    set_output("changed", "true")
    for r in keep:
        print(f"#{r['rank']} {r['name']}{' (toi)' if r['me'] else ''} : "
              f"{r['points']} pts, {r['cards']} cartes")


def report():
    if not os.path.exists(CSV_FILE):
        sys.exit(f"{CSV_FILE} introuvable.")
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    if not data:
        sys.exit("Aucun instantané.")
    stamps = sorted({d["ts_utc"] for d in data})
    t0, t1 = datetime.fromisoformat(stamps[0]), datetime.fromisoformat(stamps[-1])
    hours = max((t1 - t0).total_seconds() / 3600, 1e-9)
    print(f"{len(stamps)} instantané(s) du {stamps[0]} au {stamps[-1]} ({hours:.1f} h)\n")
    print(f"{'joueur':<18}{'pts début':>10}{'pts fin':>9}{'Δ pts':>7}{'pts/h':>7}"
          f"{'cartes':>8}{'Δ cartes':>10}")
    by = {}
    for d in data:
        by.setdefault(d["name"], []).append(d)
    for name, rows in sorted(by.items(), key=lambda kv: -int(kv[1][-1]["points"])):
        a, b = rows[0], rows[-1]
        dp = int(b["points"]) - int(a["points"])
        dc = int(b["cards"]) - int(a["cards"])
        rate = dp / hours if len(rows) > 1 else 0
        label = name + (" (toi)" if b["me"] == "1" else "")
        print(f"{label:<18}{a['points']:>10}{b['points']:>9}{dp:>+7}{rate:>7.1f}"
              f"{b['cards']:>8}{dc:>+10}")


if __name__ == "__main__":
    report() if "--report" in sys.argv else snapshot()
