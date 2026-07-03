#!/usr/bin/env python3
"""
DNNK Webinar Auto-Transskription
Overvåger DNNK's vidensbank og transskriberer nye webinarer
Transskriberer alle videoer der ikke allerede er behandlet

State-format (processed_videos.json):
  {"<video_id>": {"status": "done"|"failed"|"pending",
                  "attempts": <int>, "order_id": "...", "last_attempt": "..."}}
Gamle filer med en ren liste af video-id'er migreres automatisk til "done".
"pending" = betalt ordre afgivet men resultat ikke hentet endnu (fx timeout)
— den hentes færdig ved næste kørsel i stedet for at bestille (og betale) igen.
"""

import re
import requests
from bs4 import BeautifulSoup
import json
import os
import time
from datetime import datetime
from pathlib import Path

# Konfiguration
TRANSKRIPTOR_API_KEY = os.environ.get('TRANSKRIPTOR_API_KEY')
TRANSCRIPTIONS_FOLDER = Path("transcriptions")
PROCESSED_VIDEOS_FILE = "processed_videos.json"

MAX_ATTEMPTS = 3          # opgiv en video efter 3 fejlede forsøg
# Loft over nye betalte ordrer pr. kørsel — kontoen har rigeligt med
# minutter, så loftet er kun en nødbremse mod løbske scrape-fejl.
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "25"))
COLLECT_BUDGET_MINUTES = int(os.environ.get("COLLECT_BUDGET_MINUTES", "240"))
POLL_INTERVAL = 20        # sekunder mellem status-tjek

CATEGORIES = {
    "Tech_Talks":           "https://www.dnnk.dk/tech-talks/",
    "Godmorgen_med_DNNK":   "https://www.dnnk.dk/god-morgen-med-dnnk/",
    "Konferencer":          "https://www.dnnk.dk/optagelser-fra-konferencer-og-temadage/",
    "Jura":                 "https://www.dnnk.dk/jura-i-klimatilpasning/",
    "DNNK_Masterclass":     "https://www.dnnk.dk/dnnk-masterclass/",
    "Fremtidsvaerksted":    "https://www.dnnk.dk/fremtid/",
    "Arrangementer":        "https://www.dnnk.dk/arrangementer/",
    "Vidensbank":           "https://www.dnnk.dk/category/vidensbank/",
    "Studieture":           "https://www.dnnk.dk/online-studietur/",
    "VIP":                  "https://www.dnnk.dk/dnnk-vip/",
    "Oevrige":              "https://www.dnnk.dk/dnnk-arrangementer/"
}

def load_state():
    if os.path.exists(PROCESSED_VIDEOS_FILE):
        with open(PROCESSED_VIDEOS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            # Migrér gammelt format (liste af id'er) til dict
            return {vid: {"status": "done"} for vid in data}
        return data
    return {}

def save_state(state):
    with open(PROCESSED_VIDEOS_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def mark(state, video_id, status, **extra):
    entry = state.get(video_id, {})
    entry["status"] = status
    entry["last_attempt"] = datetime.now().isoformat(timespec="seconds")
    if status == "failed":
        entry["attempts"] = entry.get("attempts", 0) + 1
    entry.update(extra)
    state[video_id] = entry
    save_state(state)

YOUTUBE_ID_RE = re.compile(r'(?:youtu\.be/|[?&]v=|embed/)([A-Za-z0-9_-]{11})')

def extract_youtube_id(url):
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None

def _extract_video_ids(soup):
    ids = []
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '')
        if 'youtube.com' in src or 'youtu.be' in src:
            vid = extract_youtube_id(src)
            if vid:
                ids.append(vid)
    for link in soup.find_all('a', href=True):
        href = link['href']
        if 'youtube.com' in href or 'youtu.be' in href:
            vid = extract_youtube_id(href)
            if vid:
                ids.append(vid)
    return ids


MAX_SUBPAGES = 20  # undersider pr. kategori (Masterclass har én side pr. event)


def scrape_category_for_videos(category_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(category_url, timeout=30, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        youtube_urls = _extract_video_ids(soup)

        # Nogle kategorier (fx Masterclass) har videoerne på én underside
        # pr. event i stedet for på kategorisiden — crawl undersiderne.
        sub_urls = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            if ('dnnk.dk' in href and href.rstrip('/') != category_url.rstrip('/')
                    and not href.lower().endswith(('.pdf', '.jpg', '.png'))
                    and '#' not in href and '/category/' not in href
                    and '/page/' not in href):
                sub_urls.append(href)
        for sub_url in sorted(set(sub_urls))[:MAX_SUBPAGES]:
            try:
                sub = requests.get(sub_url, timeout=15, headers=headers)
                sub_soup = BeautifulSoup(sub.content, 'html.parser')
                youtube_urls.extend(_extract_video_ids(sub_soup))
            except requests.RequestException:
                pass
            time.sleep(0.5)  # høflig pause

        return list(set(youtube_urls))
    except requests.RequestException as e:
        print(f"❌ Fejl ved scraping af {category_url}: {e}")
        return []

def api_headers():
    return {
        "Authorization": f"Bearer {TRANSKRIPTOR_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def start_order(video_url):
    """Afgiv transskriptionsordre. Returnerer order_id eller None."""
    start_url = "https://api.tor.app/developer/transcription/url"
    payload = {"url": video_url, "language": "da-DK"}
    try:
        response = requests.post(start_url, headers=api_headers(), json=payload, timeout=30)
        response.raise_for_status()
        order_id = response.json().get('order_id')
        if not order_id:
            print(f"❌ Intet order_id i svar: {response.json()}")
        return order_id
    except requests.RequestException as e:
        print(f"❌ Fejl ved ordreafgivelse: {e}")
        return None

def fetch_order_content(order_id):
    """Hent færdig transskription. Returnerer tekst eller None."""
    content_url = f"https://api.tor.app/developer/files/{order_id}/content"
    try:
        resp = requests.get(content_url, headers=api_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        text = data.get('content') or data.get('text')
        if not text:
            print(f"❌ Hverken 'content' eller 'text' i API-svar (nøgler: {list(data)})")
        return text
    except requests.RequestException as e:
        print(f"❌ Fejl ved hentning af indhold: {e}")
        return None

def save_transcription(video_id, transcription, category):
    TRANSCRIPTIONS_FOLDER.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = TRANSCRIPTIONS_FOLDER / f"{category}_{video_id}_{timestamp}.txt"
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"=== DNNK Webinar Transskription ===\n")
        f.write(f"Kategori: {category}\n")
        f.write(f"Video ID: {video_id}\n")
        f.write(f"URL: https://youtube.com/watch?v={video_id}\n")
        f.write(f"Transskriberet: {datetime.now().isoformat()}\n")
        f.write(f"\n{'='*50}\n\n")
        f.write(transcription)
    print(f"✅ Transskription gemt: {filename}")
    return filename

def check_order(order_id):
    """Ét status-tjek uden ventetid.
    Returnerer ('completed', tekst) / ('failed', None) / ('working', None)."""
    status_url = f"https://api.tor.app/developer/transcription/{order_id}"
    try:
        resp = requests.get(status_url, headers=api_headers(), timeout=30)
        resp.raise_for_status()
        status = resp.json().get('status', '').lower()
    except (requests.RequestException, ValueError) as e:
        print(f"      ⚠️ Status-tjek fejlede ({e})")
        return 'working', None
    if status == 'completed':
        return 'completed', fetch_order_content(order_id)
    if status in ('error', 'failed'):
        return 'failed', None
    return 'working', None

def collect_pending(state, budget_minutes=COLLECT_BUDGET_MINUTES):
    """Rundgangs-poll af ALLE afventende ordrer til de er færdige eller
    tidsbudgettet er brugt. Ordrer der ikke når det, forbliver pending
    og samles ind ved næste kørsel — der betales aldrig igen."""
    deadline = time.time() + budget_minutes * 60
    while time.time() < deadline:
        pending = {vid: e for vid, e in state.items()
                   if e.get("status") == "pending" and e.get("order_id")}
        if not pending:
            return
        print(f"\n♻️  {len(pending)} betalte ordrer afventer — tjekker...")
        for video_id, entry in pending.items():
            status, text = check_order(entry["order_id"])
            if status == 'completed' and text:
                save_transcription(video_id, text, entry.get("category", "Oevrige"))
                mark(state, video_id, "done", order_id=None)
            elif status == 'failed':
                print(f"❌ Ordren for {video_id} fejlede hos Transkriptor")
                mark(state, video_id, "failed", order_id=None)
            time.sleep(2)
        if any(e.get("status") == "pending" for e in state.values()):
            time.sleep(POLL_INTERVAL)
    left = sum(1 for e in state.values() if e.get("status") == "pending")
    if left:
        print(f"⏰ Tidsbudget brugt — {left} ordrer hentes færdige ved næste kørsel")

def main():
    print(f"\n{'='*60}")
    print(f"🔍 Starter tjek for nye webinarer - {datetime.now()}")
    print(f"{'='*60}\n")

    if not TRANSKRIPTOR_API_KEY:
        raise SystemExit("❌ TRANSKRIPTOR_API_KEY mangler!")

    state = load_state()
    save_state(state)  # persistér evt. format-migrering med det samme

    # 1) Afgiv ordrer for ALLE nye videoer med det samme — Transkriptor
    #    transskriberer dem parallelt, mens vi venter samlet bagefter.
    orders_placed = 0
    for category_name, category_url in CATEGORIES.items():
        print(f"\n📂 Tjekker kategori: {category_name}")
        video_ids = scrape_category_for_videos(category_url)
        print(f"   Fandt {len(video_ids)} videoer i alt")

        for video_id in video_ids:
            entry = state.get(video_id)
            if entry:
                if entry.get("status") in ("done", "pending"):
                    continue
                if entry.get("attempts", 0) >= MAX_ATTEMPTS:
                    continue  # opgivet — undgå at betale for samme fejl hver dag

            if orders_placed >= MAX_NEW_PER_RUN:
                print(f"   ⏸️ Loft på {MAX_NEW_PER_RUN} nye ordrer nået — resten tages næste kørsel")
                break

            print(f"   🆕 Ny video: {video_id} — afgiver ordre...")
            order_id = start_order(f"https://youtube.com/watch?v={video_id}")
            if order_id:
                mark(state, video_id, "pending", order_id=order_id, category=category_name)
                orders_placed += 1
            else:
                mark(state, video_id, "failed")
            time.sleep(2)
        else:
            continue
        break  # loftet er nået — stop også ydre løkke

    # 2) Saml alle færdige transskriptioner ind (også fra tidligere kørsler)
    collect_pending(state)

    done_now = sum(1 for e in state.values() if e.get("status") == "done")
    print(f"\n{'='*60}")
    print(f"✅ Kørsel slut — {orders_placed} nye ordrer afgivet, {done_now} videoer færdige i alt")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
