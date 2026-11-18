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

- **Doublons** : EPIC et COMMON sont recyclées. Les RARE sont mises en vente à prix
  fixe (un cran sous l'annonce la moins chère, entre 20 et 30 essecoins) ; si elles ne
  sont pas vendues après 120 min (`SELL_TIMEOUT_MIN`), l'annonce est retirée et la
  carte recyclée. Seules les annonces créées par le bot sont suivies (fichier
  `sell_state.json`, conservé entre les runs avec `actions/cache`). LEGENDARY et
  CREATOR ne sont jamais touchées. Une carte dont un exemplaire est déjà en vente ou
  en échange n'est jamais recyclée.
- **Achats** : cartes manquantes (« New for you ») classées par points au classement
  par essecoin (commune 1, rare 3, épique 8), sous les seuils epic ≤ 60, rare < 25,
  common < 15 et avec au moins `MIN_RATIO` = 0,125 point par essecoin.
- **Enchères** : dans la dernière minute, mise minimale autorisée par le site (≈ +5 %)
  sur les cartes manquantes qui passent la même règle de prix ; remise si dépassé (4
  mises max par enchère).

Variables du dépôt (Settings > Secrets and variables > Actions > Variables) :

| Variable | Effet |
|---|---|
| `MARKET_DRY_RUN` | `false` = recyclage et achats réels, sinon simulation |
| `MARKET_SELL_DRY_RUN` | `false` en plus = mise en vente réelle des rares (sinon simulée, les rares sont recyclées) |
| `MARKET_AUCTION_DRY_RUN` | `false` = mises réelles, sinon simulation |
| `MARKET_MIN_RATIO` | points par essecoin minimum pour acheter (0,125 par défaut) |

Le lancement manuel du workflow accepte `dry_run` et `min_ratio` pour tester sans
toucher aux variables.
