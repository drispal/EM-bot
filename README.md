# Boosters Essec Masters via GitHub Actions

## Mise en place

1. Crée un dépôt **public** sur GitHub (gratuit et illimité en minutes d'Actions).
2. Mets-y ces fichiers :
   - `open_booster.py` à la racine
   - `login_local.py` à la racine
   - `booster.yml` dans `.github/workflows/booster.yml`
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
