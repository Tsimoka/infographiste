#!/usr/bin/env python3
"""
Extraction des résultats VISIBLES d'une recherche Google Maps — SANS API.
Version RAPIDE : traite plusieurs villes EN PARALLÈLE (plusieurs onglets
dans le même navigateur) au lieu d'une ville à la fois.

⚠️ Important :
- Toujours pas l'API officielle Google : automatisation de navigateur.
- Nombre de fenêtres/onglets en parallèle réduit pour éviter les bans IP 
  et surcharges sur l'infrastructure GitHub Actions.
- Intégration d'une sécurité temporelle (Time-out propre) pour éviter
  la corruption des fichiers CSV en fin de job.

Lancement :
    python scrape_wellness_rapide.py
"""

import asyncio
import csv
import os
import re
import sys
import random
import time
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ============================================================
# CONFIGURATION
# ============================================================

VILLES_CSV = "villes.csv"
RESULTATS_CSV = "resultats_visibles.csv"

REQUETES = [
    ("hôtel spa",       "Hôtel avec spa"),
    ("centre thermal",  "Centre thermal"),
    ("parc aquatique",  "Parc aquatique"),
]

COLONNES_RESULTATS = [
    "Nom de l'établissement", "Note", "Nombre d'avis", "Catégorie",
    "Adresse / résumé", "Ville", "Activité", "URL Google Maps",
]

HEADLESS = True
MAX_ETABLISSEMENTS_PAR_REQUETE = 25

# Nombre de villes traitées EN MÊME TEMPS (chacune dans son propre onglet).
# Réduit de 15 à 6 pour éviter les blocages immédiats de Google et l'erreur de commit.
CONCURRENCE = 6 
PAUSE_COURTE = (0.5, 1.0)
PAUSE_ENTRE_VILLES = (1.5, 3.0)  
PAUSE_SCROLL = (0.6, 1.2)

# Sécurité temporelle pour GitHub Actions (Arrêt propre avant les 90 minutes du workflow)
HEURE_DE_DEBUT = time.time()
LIMITE_TEMPS_SECONDES = 80 * 60  # 80 minutes max

RATING_REGEX = re.compile(r"^\d[.,]\d$")
REVIEWS_REGEX = re.compile(r"^\(([\d\s.,]+)\)$")

RESSOURCES_A_BLOQUER = {"image", "media", "font", "stylesheet"}
DOMAINES_A_BLOQUER = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "fonts.gstatic.com", "fonts.googleapis.com",
    "gstatic.com/og", "play.google.com/log",
)


async def bloquer_ressources_inutiles(route):
    req = route.request
    if req.resource_type in RESSOURCES_A_BLOQUER:
        return await route.abort()
    url = req.url.lower()
    if any(d in url for d in DOMAINES_A_BLOQUER):
        return await route.abort()
    await route.continue_()


async def pause(bornes):
    await asyncio.sleep(random.uniform(*bornes))


# ============================================================
# Lecture / écriture fichiers
# ============================================================

def read_text_any_encoding(path):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read(), enc
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Impossible de décoder {path} avec les encodages testés.")


def load_cities(path):
    if not os.path.exists(path):
        # Crée un fichier d'exemple si absent
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            f.write("ville,statut\nParis,\nLyon,\nMarseille,\n")
    
    content, enc_used = read_text_any_encoding(path)
    print(f"[i] {path} lu avec l'encodage : {enc_used}")
    reader = csv.DictReader(content.splitlines())
    rows = list(reader)
    fieldnames = reader.fieldnames

    col_ville = None
    for c in fieldnames:
        if c.strip().lower() in ("ville", "city", "villes"):
            col_ville = c
            break
    if col_ville is None:
        col_ville = fieldnames[0]

    if "statut" not in fieldnames:
        fieldnames = list(fieldnames) + ["statut"]
        for r in rows:
            r["statut"] = ""

    return rows, fieldnames, col_ville


def save_cities(path, rows, fieldnames, max_tentatives=5):
    import time as _time
    for tentative in range(1, max_tentatives + 1):
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            return True
        except PermissionError:
            if tentative == 1:
                print(f"     [!] {path} semble ouvert dans un autre programme. Nouvelle tentative dans 3s...")
            _time.sleep(3)
    print(f"     [!!] Impossible d'écrire {path} après {max_tentatives} tentatives.")
    return False


def init_resultats_file():
    if not os.path.exists(RESULTATS_CSV):
        with open(RESULTATS_CSV, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=COLONNES_RESULTATS)
            writer.writeheader()


def write_row_to_csv(row_data):
    with open(RESULTATS_CSV, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLONNES_RESULTATS)
        writer.writerow(row_data)


def mark_done(rows, fieldnames, col_ville, city, path):
    for r in rows:
        if r.get(col_ville, "").strip() == city:
            r["statut"] = f"traité ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    save_cities(path, rows, fieldnames)


# ============================================================
# Extraction depuis la liste de résultats
# ============================================================

def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_card_text(raw_text):
    lignes = [clean_text(l) for l in raw_text.split("\n") if clean_text(l)]

    nom = lignes[0] if lignes else ""
    note = ""
    avis = ""
    reste = []

    for l in lignes[1:]:
        if not note and RATING_REGEX.match(l):
            note = l.replace(",", ".")
            continue
        m = REVIEWS_REGEX.match(l)
        if not avis and m:
            avis = m.group(1).replace(" ", "").replace("\u202f", "")
            continue
        reste.append(l)

    categorie = ""
    adresse = ""
    if reste:
        premiere_ligne_utile = reste[0]
        parts = [p.strip() for p in premiere_ligne_utile.split("·")]
        if parts:
            categorie = parts[0]
            adresse = " · ".join(parts[1:]) if len(parts) > 1 else ""

    return nom, note, avis, categorie, adresse


async def search_and_scrape(page, query, city, activite):
    search_text = f"{query} à {city}"
    url = f"https://www.google.com/maps/search/{search_text.replace(' ', '+')}/?hl=fr"
    print(f"  [{city}] -> {activite}")

    try:
        await page.goto(url, timeout=20000)
    except PWTimeout:
        print(f"     [!] [{city}] timeout au chargement, on passe")
        return

    await pause(PAUSE_COURTE)

    try:
        results_panel = page.locator('div[role="feed"]').first
        await results_panel.wait_for(timeout=7000)
    except PWTimeout:
        print(f"     [!] [{city}] pas de panneau de résultats pour '{city}' (0 résultat ou blocage/captcha)")
        return

    previous_count = 0
    stagnant_rounds = 0
    while stagnant_rounds < 2:
        cards = await results_panel.locator("a.hfpxzc").all()
        if len(cards) >= MAX_ETABLISSEMENTS_PAR_REQUETE:
            break
        if len(cards) == previous_count:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        previous_count = len(cards)

        await results_panel.evaluate("(el) => el.scrollBy(0, 800)")
        await pause(PAUSE_SCROLL)

    cards = (await results_panel.locator("a.hfpxzc").all())[:MAX_ETABLISSEMENTS_PAR_REQUETE]
    print(f"     [{city}] {len(cards)} établissement(s) repérés pour '{activite}'")

    for card_link in cards:
        href = await card_link.get_attribute("href")
        if not href:
            continue

        try:
            raw_text = await card_link.inner_text()
            nom, note, avis, categorie, adresse = parse_card_text(raw_text)
            
            if nom:
                row_data = {
                    "Nom de l'établissement": nom,
                    "Note": note,
                    "Nombre d'avis": avis,
                    "Catégorie": categorie,
                    "Adresse / résumé": adresse,
                    "Ville": city,
                    "Activité": activite,
                    "URL Google Maps": href
                }
                write_row_to_csv(row_data)
        except Exception as e:
            print(f"     [!] Erreur lors de la lecture d'une carte à {city} : {e}")
            continue


async def worker(queue, browser, rows, fieldnames, col_ville, lock):
    # Chaque worker gère son propre onglet/contexte pour isoler les requêtes
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = await context.new_page()
    await page.route("**/*", bloquer_ressources_inutiles)

    while True:
        # Sécurité temporelle anti-crash globale
        if time.time() - HEURE_DE_DEBUT > LIMITE_TEMPS_SECONDES:
            print(f"[Worker] Alerte limite de temps atteinte. Fermeture de l'onglet.")
            queue.task_done()
            break

        try:
            city = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        print(f"[+] Traitement de la ville : {city}")
        
        # Exécution des différentes requêtes pour cette ville
        for query, activite in REQUETES:
            await search_and_scrape(page, query, city, activite)
            await pause(PAUSE_COURTE)

        # Verrouillage asynchrone pour éviter les conflits d'écriture simultanés sur villes.csv
        async with lock:
            mark_done(rows, fieldnames, col_ville, city, VILLES_CSV)

        await pause(PAUSE_ENTRE_VILLES)
        queue.task_done()

    await context.close()


async def main():
    print("[i] Démarrage du script de scraping rapide...")
    init_resultats_file()
    
    rows, fieldnames, col_ville = load_cities(VILLES_CSV)
    villes_a_traiter = [r[col_ville].strip() for r in rows if not r.get("statut", "").startswith("traité")]
    
    if not villes_a_traiter:
        print("[i] Toutes les villes ont déjà été traitées dans villes.csv !")
        return

    print(f"[i] {len(villes_a_traiter)} ville(s) restante(s) à traiter.")

    queue = asyncio.Queue()
    for v in villes_a_traiter:
        queue.put_nowait(v)

    lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        
        # Création des workers parallèles selon la configuration de CONCURRENCE
        tasks = []
        num_workers = min(CONCURRENCE, len(villes_a_traiter))
        print(f"[i] Lancement de {num_workers} onglet(s) en parallèle.")
        
        for _ in range(num_workers):
            task = asyncio.create_task(worker(queue, browser, rows, fieldnames, col_ville, lock))
            tasks.append(task)

        await queue.join()
        
        # Annulation des workers si la file s'est arrêtée suite à la limite de temps
        for task in tasks:
            task.cancel()
            
        await browser.close()
    
    print(f"[i] Session terminée. Temps écoulé : {round((time.time() - HEURE_DE_DEBUT)/60, 2)} minutes.")


if __name__ == "__main__":
    # Correctif pour la boucle d'événements asynchrones sur Windows si exécuté en local
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
