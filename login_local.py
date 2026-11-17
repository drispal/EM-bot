"""À lancer UNE FOIS sur ton ordinateur.

Ouvre un navigateur : tu te connectes toi-même à Essec Masters. Le script
enregistre ensuite la session dans storage_state.json (à mettre dans le
secret GitHub ESSEC_STORAGE_STATE). Ne commit jamais ce fichier.
"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(locale="en-US")
    page = context.new_page()
    page.goto("https://www.essecmasters.com/login")
    print("Connecte-toi dans la fenêtre qui vient de s'ouvrir (5 minutes max)...")
    page.wait_for_url("**/home", timeout=300_000)
    page.wait_for_timeout(2000)
    context.storage_state(path="storage_state.json")
    print("Session enregistrée dans storage_state.json")
    browser.close()
