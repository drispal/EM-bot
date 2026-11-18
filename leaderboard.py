"""Suivi du classement Essec Masters : points et cartes des 3 premiers (et de toi).

À chaque passage (au plus un instantané toutes les TRACK_EVERY_MIN minutes), lit
/leaderboard et ajoute des lignes « ts_utc,rank,name,cards,points,me » à un fichier CSV.
Dans le workflow, ce CSV vit sur la branche « tracking » (voir booster.yml).

  python leaderboard.py             instantané (écrit TRACK_CSV)
  python leaderboard.py --report    rapport (points/heure, écart avec les suivants) à partir
                                    du CSV, sans navigateur ; le même rapport en Markdown est écrit
                                    dans REPORT.md et dans le résumé du run GitHub

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
        emit_report(CSV_FILE, write_file=False)
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
    emit_report(CSV_FILE, write_file=True)
    for r in keep:
        print(f"#{r['rank']} {r['name']}{' (toi)' if r['me'] else ''} : "
              f"{r['points']} pts, {r['cards']} cartes")


def load(path):
    """{timestamp: {name: {rank, cards, points, me}}} trié par date."""
    with open(path, newline="", encoding="utf-8") as f:
        data = list(csv.DictReader(f))
    snaps = {}
    for d in data:
        snaps.setdefault(datetime.fromisoformat(d["ts_utc"]), {})[d["name"]] = {
            "rank": int(d["rank"]), "cards": int(d["cards"]),
            "points": int(d["points"]), "me": d["me"] == "1"}
    return dict(sorted(snaps.items()))


def rate(snaps, name, hours=None, field="points"):
    """Points (ou cartes) gagnés par heure sur la fenêtre `hours` (None = tout l'historique).
    Renvoie (taux, durée réelle en h) ou (None, 0) si moins de deux instantanés."""
    pts = [(t, v[name][field]) for t, v in snaps.items() if name in v]
    if hours is not None and pts:
        limit = pts[-1][0].timestamp() - hours * 3600
        window = [p for p in pts if p[0].timestamp() >= limit]
        pts = window if len(window) >= 2 else pts[-2:]
    if len(pts) < 2:
        return None, 0
    span = (pts[-1][0] - pts[0][0]).total_seconds() / 3600
    if span <= 0:
        return None, 0
    return (pts[-1][1] - pts[0][1]) / span, span


def fmt(x, signed=False):
    if x is None:
        return "n/a"
    return f"{x:+.1f}" if signed else f"{x:.1f}"


def build_report(snaps):
    """Rapport Markdown : tes points par heure et l'écart avec les suivants."""
    times = list(snaps)
    last = snaps[times[-1]]
    me = next((n for n, v in last.items() if v["me"]), None)
    if me is None:
        return "Aucune ligne « toi » dans le dernier instantané."
    out = ["# Suivi du classement",
           f"{len(times)} instantané(s), du {times[0]:%d/%m %H:%M} au {times[-1]:%d/%m %H:%M} UTC "
           f"({(times[-1] - times[0]).total_seconds() / 3600:.1f} h).", ""]

    windows = [("depuis le début", None), ("3 dernières h", 3), ("dernière h", 1)]
    out += ["## Tes points par heure", "| Fenêtre | Points/h | Cartes/h | Durée mesurée |", "|---|---|---|---|"]
    for label, h in windows:
        r, span = rate(snaps, me, h)
        c, _ = rate(snaps, me, h, "cards")
        out.append(f"| {label} | {fmt(r)} | {fmt(c)} | {span:.1f} h |" if r is not None
                   else f"| {label} | n/a | n/a | pas assez de données |")
    out.append("")

    others = sorted((n for n in last if n != me), key=lambda n: last[n]["rank"])
    out += ["## Les autres du podium face à toi",
            "| Joueur | Rang | Points | Écart avec toi | Ses pts/h (début) | Ses pts/h (3 h) "
            "| Écart/h pour toi (début) | Rattrapage |", "|---|---|---|---|---|---|---|---|"]
    my_all, _ = rate(snaps, me)
    for n in others:
        gap = last[me]["points"] - last[n]["points"]
        r_all, _ = rate(snaps, n)
        r_3h, _ = rate(snaps, n, 3)
        if my_all is None or r_all is None:
            trend, verdict = "n/a", "pas assez de données"
        else:
            diff = my_all - r_all  # >0 : tu creuses l'écart
            trend = fmt(diff, signed=True)
            if gap <= 0:
                verdict = "te devance déjà" if gap < 0 else "à égalité"
            elif diff >= 0:
                verdict = "ne te rattrape pas"
            else:
                verdict = f"rattrapage dans ≈ {gap / -diff:.0f} h"
        out.append(f"| {n} | {last[n]['rank']} | {last[n]['points']} | {gap:+d} | {fmt(r_all)} "
                   f"| {fmt(r_3h)} | {trend} | {verdict} |")
    out += ["", f"Toi : {last[me]['points']} pts, {last[me]['cards']} cartes (rang {last[me]['rank']}).",
            "Rattrapage = durée estimée avant qu'il te dépasse si les rythmes actuels se maintiennent "
            "(les gains par heure fluctuent beaucoup d'une heure à l'autre)."]
    return "\n".join(out)


def emit_report(path, write_file):
    snaps = load(path)
    text = build_report(snaps)
    if write_file:
        with open(os.path.join(os.path.dirname(path) or ".", "REPORT.md"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return text


def report():
    if not os.path.exists(CSV_FILE):
        sys.exit(f"{CSV_FILE} introuvable.")
    print(emit_report(CSV_FILE, write_file=False))


if __name__ == "__main__":
    report() if "--report" in sys.argv else snapshot()
