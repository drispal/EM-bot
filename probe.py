"""Sonde en lecture seule : cartes communes et rares qui me manquent, en vente jusqu'à un prix donné.

Ne clique que sur « Show more listings » (jamais sur « Buy »). Écrit un tableau dans le résumé du run.
Variables : PROBE_LIMIT (prix maximal lu, 100 par défaut).
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

import market as M

LIMIT = int(os.environ.get("PROBE_LIMIT", "100"))
RARITIES = ("common", "rare")


def main():
    raw = os.environ.get("ESSEC_STORAGE_STATE")
    if not raw:
        sys.exit("Secret ESSEC_STORAGE_STATE manquant.")
    lines = [f"## Sonde marché : cartes qui me manquent, en vente jusqu'à {LIMIT} essecoins (aucun achat)"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(storage_state=json.loads(raw), locale="en-US",
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        for rarity in RARITIES:
            got = M.fetch_listings(page, rarity, LIMIT)
            new = [l for l in got if l["is_new"]]
            print(f"{rarity} : {len(got)} annonce(s) lues jusqu'à {LIMIT}, {len(new)} carte(s) manquante(s)")
            lines.append(f"\n### {rarity} : {len(got)} annonce(s) lues, {len(new)} pour une carte qui me manque")
            if got:
                prices = sorted(l["price"] for l in got)
                lines.append(f"Prix des annonces lues : de {prices[0]} à {prices[-1]} "
                             f"(médiane {prices[len(prices) // 2]}).")
            best = M.cheapest_by_name(new)
            best.sort(key=lambda l: l["price"])
            if best:
                pts = M.POINTS[rarity]
                lines.append("| Carte | Prix | Points | Essecoins par point |\n|---|---|---|---|")
                for l in best:
                    lines.append(f"| {l['name']} | {l['price']} | {pts} | {l['price'] / pts:.0f} |")
                    print(f"  {rarity} « {l['name']} » à {l['price']} essecoins ({l['price'] / pts:.0f}/pt)")
            else:
                lines.append("Aucune.")
        browser.close()
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
