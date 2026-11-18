# Boosters Essec Masters via GitHub Actions

## Mise en place

1. Crée un dépôt **public** sur GitHub (gratuit et illimité en minutes d'Actions).
2. Mets-y ces fichiers :
   - `open_booster.py` à la racine
   - `login_local.py` à la racine
   - `booster.yml` dans `.github/workflows/booster.yml`
   - `market.py`, `auctions.py`, `requirements.txt` et `.github/actions/setup/action.yml`
   - un `.gitignore` contenant la ligne `storage_state.json`
3. Sur ton ordinateur :
   ```
   pip install playwright
   playwright install chromium
   python login_local.py
   ```
   Connecte-toi toi-même dans la fenêtre qui s'ouvre. Le fichier
   `storage_state.json` est créé (il contient ta session : ne le partage pas,
   ne le commit pas).
4. Sur GitHub : Settings > Secrets and variables > Actions > New repository
   secret. Nom : `ESSEC_STORAGE_STATE`, valeur : le contenu complet de
   `storage_state.json`.
5. Onglet Actions > "Open Essec Masters booster" > Run workflow pour tester.
   Les logs affichent les cartes obtenues et le compteur de collection.

## À savoir

- Le workflow se lance toutes les 5 minutes. Un booster n'est ouvert que s'il
  est disponible, sinon le log indique le temps restant.
- GitHub retarde ou saute parfois des exécutions planifiées : les ouvertures
  peuvent être décalées de quelques minutes.
- Si la session expire, l'exécution échoue avec "Session expirée" (GitHub
  t'envoie un email). Relance `login_local.py` et remplace le secret.
- GitHub désactive les workflows planifiés d'un dépôt public sans activité
  pendant 60 jours : réactive-les dans l'onglet Actions si besoin.
- Ne mets jamais de mot de passe ou de contenu de `storage_state.json` dans le
  code ou dans les logs.

## Marché et enchères

`market.py` et `auctions.py` tournent à chaque lancement du workflow, qu'un booster
soit disponible ou non (`auctions.py` est un job séparé qui surveille les enchères qui
se terminent dans les 6 minutes).

- **Doublons** : les COMMON sont recyclées. Les RARE (20 à 30 essecoins) sont mises en
  vente à prix fixe, un cran sous l'annonce la moins chère. Les EPIC (50 à 62) sont mises
  au dernier prix de vente (« last sold »), un cran sous l'annonce la moins chère si elle
  est plus basse, plafonné à 62 (c'est là qu'elles se vendent). Si elles ne sont pas vendues
  après 120 min (`SELL_TIMEOUT_MIN`), l'annonce est retirée et
  la carte recyclée. Seules les annonces créées par le bot sont suivies (fichier
  `sell_state.json`, conservé entre les runs avec `actions/cache`). LEGENDARY et
  CREATOR ne sont jamais recyclées ni vendues. Une carte dont un exemplaire est déjà en
  vente ou en échange n'est jamais recyclée.
- **Achat-revente** : une annonce d'une carte que l'on a déjà, à un prix inférieur à sa
  valeur de recyclage (au moins 2 essecoins de gain pour une commune, 3 pour une rare,
  5 pour une épique), est achetée (20 par exécution au maximum) ; le doublon est ensuite recyclé ou mis en vente au passage suivant, comme les autres.
- **Achats** : cartes manquantes (« New for you ») classées par points au classement
  par essecoin (commune 1, rare 3, épique 8, légendaire 20), sous les seuils common < 15,
  rare < 25, epic ≤ 70, legendary ≤ 150 et avec au moins `MIN_RATIO` = 0,125 point par
  essecoin (0,11 pour les épiques). Si une carte intéressante n'est pas encore abordable, le bot met de côté au
  lieu de dépenser pour des cartes moins rentables. Ensuite, pour ne jamais laisser dormir
  les essecoins, le ratio minimum descend à `RELAXED_RATIO` (0,08 : épiques jusqu'à ≈100,
  légendaires jusqu'à ≈250, communes jusqu'à 12), sans réserve (`IDLE_BALANCE` = 0,
  `RESERVE` = 0) ; on n'attend une meilleure carte que si elle est à moins de 40 essecoins du
  solde. Si le solde dépasse `FLOOR_BALANCE` (200) malgré tout, dernier palier à
  `FLOOR_RATIO` (0,05 : épiques jusqu'à ≈160, légendaires jusqu'à ≈400).
- **Enchères** : dans la dernière minute, mise minimale autorisée par le site (≈ +5 %)
  sur les cartes manquantes qui passent la même règle de prix (y compris la règle assouplie
  quand le solde le permet, palier à 200 inclus) ; remise si dépassé (4
  mises max par enchère).

- **Suivi du classement** : à chaque passage des enchères, le script `leaderboard.py` lit
  `/leaderboard` et, au plus toutes les 30 min, ajoute les 3 premiers (rang, points, cartes) et
  toi si tu n'es pas dans le top 3 au fichier `leaderboard.csv` de la branche `tracking`
  (`git fetch origin tracking && git show origin/tracking:leaderboard.csv`). Le rapport
  (tes points/heure, ceux des autres, écart et durée estimée avant un éventuel rattrapage) est
  dans `REPORT.md` sur la même branche, dans le résumé de chaque run des enchères, ou avec
  `TRACK_CSV=leaderboard.csv python leaderboard.py --report`.

Variables du dépôt (Settings > Secrets and variables > Actions > Variables) :

| Variable | Effet |
|---|---|
| `MARKET_DRY_RUN` | `false` = recyclage et achats réels, sinon simulation |
| `MARKET_SELL_DRY_RUN` | `false` en plus = mise en vente réelle des rares (sinon simulée, les rares sont recyclées) |
| `MARKET_AUCTION_DRY_RUN` | `false` = mises réelles, sinon simulation |
| `MARKET_MIN_RATIO` | points par essecoin minimum pour acheter (0,125 par défaut) |

Le lancement manuel du workflow accepte `dry_run` et `min_ratio` pour tester sans
toucher aux variables.

- **Robustesse** : les pages n'attendent plus « networkidle » plus de 10 s et un chargement
  échoué est retenté une fois (booster, marché). La lecture du marché ne s'arrête que si le
  bouton « Show more listings » reste absent et que le nombre d'annonces n'est pas un multiple
  de 24 ; sinon attente de 4 s puis un seul rechargement.
