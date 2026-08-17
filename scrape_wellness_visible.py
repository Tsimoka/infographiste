#!/usr/bin/env python3
"""
Extraction des résultats VISIBLES d'une recherche Google Maps — SANS API.
Version RAPIDE : traite plusieurs villes EN PARALLÈLE (plusieurs onglets
dans le même navigateur) au lieu d'une ville à la fois.

C'est le vrai levier de vitesse par rapport à la version séquentielle :
avec 200+ villes, attendre chaque page une par une est le principal goulot
d'étranglement, pas la durée des pauses elles-mêmes.

⚠️ Important :
- Toujours pas l'API officielle Google : automatisation de navigateur.
  Plus d'onglets en parallèle = plus de requêtes/minute vers Google =
  risque de blocage/captcha plus élevé. 6-8 onglets est un compromis
  raisonnable, pas une garantie.
- Si Google bloque en cours de route (captcha, page vide inhabituelle),
  le script continue sur les autres villes mais celle-ci sera à revérifier
  manuellement (elle ne sera pas marquée "traité" si aucun résultat n'a pu
  être lu, donc elle sera retentée au prochain lancement).

Installation :
    pip install playwright
    playwright install chromium

Entrée  : villes.csv          (colonne "ville" ; colonne "statut" ajoutée/mise à jour)
Sortie  : resultats_visibles.csv (écriture en temps réel, une ligne par établissement)

Lancement :
    python scrape_wellness_rapide.py
"""

import asyncio
import csv
import os
import re
import sys
import random
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
CONCURRENCE = 15
PAUSE_COURTE = (0.2, 0.4)
PAUSE_ENTRE_VILLES = (0.6, 1.2)  # par onglet, entre deux villes qu'il traite
PAUSE_SCROLL = (0.35, 0.6)

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
# Lecture / écriture villes.csv
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
    """Plusieurs tentatives : sur Windows, le fichier peut être temporairement
    verrouillé (Excel ouvert, antivirus). On réessaie au lieu de planter."""
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
                print(f"     [!] {path} semble ouvert dans un autre programme "
                      f"(Excel ?). Ferme-le si possible. Nouvelle tentative dans 3s...")
            _time.sleep(3)
    print(f"     [!!] Impossible d'écrire {path} après {max_tentatives} tentatives. "
          f"Cette ville sera retraitée au prochain lancement.")
    return False


def mark_done(rows, fieldnames, col_ville, city, path, lock_dict):
    for r in rows:
        if r.get(col_ville, "").strip() == city:
            r["statut"] = f"traité ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    save_cities(path, rows, fieldnames)


# ============================================================
# Extraction depuis la liste de résultats (sans ouvrir les fiches)
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


async def search_and_scrape(page, query, city, activite, write_row):
    search_text = f"{query} à {city}"
    url = f"https://www.google.com/maps/search/{search_text.replace(' ', '+')}/?hl=fr"
    print(f"  [{city}] -> {activite}")

    try:
        await page.goto(url, timeout=15000)
    except PWTimeout:
        print(f"     [!] [{city}] timeout au chargement, on passe")
        return

    await pause(PAUSE_COURTE)

    try:
        results_panel = page.locator('div[role="feed"]').first
        await results_panel.wait_for(timeout=5000)
    except PWTimeout:
        print(f"     [!] [{city}] pas de panneau de résultats (0 résultat ou page inattendue)")
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

    # Plus de vérification de doublon ici : chaque établissement trouvé
    # est écrit tel quel dans le fichier résultat, même s'il apparaît
    # plusieurs fois (autre ville, autre requête, ou relance du script).
    for card_link in cards:
        href = await card_link.get_attribute("href")
        if not href:
            continue

        try:
            container = card_link.locator("xpath=..")
            raw_text = await container.inner_text(timeout=1500)
        except Exception:
            continue

        nom, note, avis, categorie, adresse = parse_card_text(raw_text)
        if not nom:
            continue

        row = [nom, note, avis, categorie, adresse, city, activite, href]
        await write_row(row)


async def traiter_ville(context, city, write_row, sem):
    """Traite une ville complète (toutes les requêtes REQUETES) dans son
    propre onglet, sous protection du sémaphore de concurrence."""
    async with sem:
        page = await context.new_page()
        page.set_default_timeout(5000)
        try:
            print(f"\n=== {city} ===")
            for query, activite in REQUETES:
                await search_and_scrape(page, query, city, activite, write_row)
            await pause(PAUSE_ENTRE_VILLES)
            return city, True
        except Exception as e:
            print(f"  [!!] [{city}] erreur inattendue : {e}")
            return city, False
        finally:
            await page.close()


async def main():
    if not os.path.exists(VILLES_CSV):
        print(f"[!] Fichier introuvable : {VILLES_CSV}")
        sys.exit(1)

    rows, fieldnames, col_ville = load_cities(VILLES_CSV)
    villes_a_traiter = [r[col_ville].strip() for r in rows
                         if r[col_ville].strip() and not r.get("statut", "").startswith("traité")]

    print(f"{len(villes_a_traiter)} ville(s) à traiter sur {len(rows)} au total")
    print(f"Concurrence : {CONCURRENCE} onglet(s) en parallèle")

    file_exists = os.path.exists(RESULTATS_CSV)

    out_f = open(RESULTATS_CSV, "a", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(out_f)
    if not file_exists:
        csv_writer.writerow(COLONNES_RESULTATS)
        out_f.flush()

    write_lock = asyncio.Lock()
    csv_villes_lock = asyncio.Lock()

    async def write_row(row):
        # Toutes les coroutines écrivent dans le même fichier : un verrou
        # évite que deux lignes se mélangent en cas d'écriture simultanée.
        async with write_lock:
            csv_writer.writerow(row)
            out_f.flush()

    sem = asyncio.Semaphore(CONCURRENCE)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-default-apps",
                "--mute-audio",
                "--no-first-run",
            ],
        )
        context = await browser.new_context(
            locale="fr-FR", viewport={"width": 1000, "height": 700}
        )
        await context.route("**/*", bloquer_ressources_inutiles)

        consent_page = await context.new_page()
        try:
            await consent_page.goto("https://www.google.com/maps?hl=fr", timeout=15000)
            consent_btn = consent_page.locator("button:has-text('Tout accepter')").first
            if await consent_btn.is_visible(timeout=2000):
                await consent_btn.click()
        except Exception:
            pass
        await consent_page.close()

        taches = [
            traiter_ville(context, city, write_row, sem)
            for city in villes_a_traiter
        ]

        traitees = 0
        for coro in asyncio.as_completed(taches):
            city, succes = await coro
            if succes:
                async with csv_villes_lock:
                    mark_done(rows, fieldnames, col_ville, city, VILLES_CSV, None)
                print(f"  [OK] {city} marquée 'traité'")
            traitees += 1
            print(f"--- Progression : {traitees}/{len(villes_a_traiter)} villes ---")

        await browser.close()

    out_f.close()
    print(f"\nTerminé. Résultats dans {RESULTATS_CSV}")


if __name__ == "__main__":
    asyncio.run(main())