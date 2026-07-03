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
MAX_NEW_PER_RUN = 5       # loft over betalte ordrer pr. kørsel (efterslæb indhentes over flere dage)
POLL_INITIAL_WAIT = 30    # sekunder før første status-tjek
POLL_INTERVAL = 20        # sekunder mellem status-tjek
POLL_MAX_MINUTES = 40     # lange webinarer tager ofte >10 min at transskribere

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

def poll_order(order_id, max_minutes=POLL_MAX_MINUTES):
    """Poll en ordre til den er færdig.
    Returnerer ('completed', tekst) / ('failed', None) / ('timeout', None)."""
    status_url = f"https://api.tor.app/developer/transcription/{order_id}"
    deadline = time.time() + max_minutes * 60
    time.sleep(POLL_INITIAL_WAIT)
    while time.time() < deadline:
        try:
            status_response = requests.get(status_url, headers=api_headers(), timeout=30)
            status_response.raise_for_status()
            status = status_response.json().get('status', '').lower()
        except (requests.RequestException, ValueError) as e:
            # Enkeltstående API-hikke må ikke vælte en betalt ordre
            print(f"      ⚠️ Status-tjek fejlede ({e}) — prøver igen")
            time.sleep(POLL_INTERVAL)
            continue

        if status == 'completed':
            return 'completed', fetch_order_content(order_id)
        elif status in ('error', 'failed'):
            print(f"❌ Ordren fejlede hos Transkriptor (status: {status})")
            return 'failed', None

        remaining = int(deadline - time.time())
        print(f"      ⏳ Venter... status: {status} (~{remaining // 60} min. tilbage)")
        time.sleep(POLL_INTERVAL)

    print(f"⏰ Timeout efter {max_minutes} min. — ordren hentes færdig ved næste kørsel")
    return 'timeout', None

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

def resume_pending(state):
    """Hent resultater for allerede betalte ordrer fra tidligere kørsler."""
    pending = {vid: e for vid, e in state.items()
               if e.get("status") == "pending" and e.get("order_id")}
    for video_id, entry in pending.items():
        print(f"\n♻️  Genoptager betalt ordre for {video_id} (order_id: {entry['order_id']})")
        status, text = poll_order(entry["order_id"], max_minutes=5)
        if status == 'completed' and text:
            save_transcription(video_id, text, entry.get("category", "Oevrige"))
            mark(state, video_id, "done")
        elif status == 'failed':
            mark(state, video_id, "failed", order_id=None)
        # timeout → forbliver pending, prøves igen næste kørsel

def process_video(state, video_id, category):
    print(f"\n   🆕 Ny video fundet: {video_id}")
    print(f"      URL: https://youtube.com/watch?v={video_id}")
    print(f"      🎤 Starter transskription...")

    order_id = start_order(f"https://youtube.com/watch?v={video_id}")
    if not order_id:
        mark(state, video_id, "failed")
        return False

    print(f"      ⏳ Transskription startet – order_id: {order_id}")
    status, text = poll_order(order_id)

    if status == 'completed' and text:
        save_transcription(video_id, text, category)
        mark(state, video_id, "done", order_id=None)
        return True
    elif status == 'timeout':
        # Ordren ER betalt — gem order_id og hent resultatet næste kørsel
        mark(state, video_id, "pending", order_id=order_id, category=category)
        return False
    else:
        mark(state, video_id, "failed")
        return False

def main():
    print(f"\n{'='*60}")
    print(f"🔍 Starter tjek for nye webinarer - {datetime.now()}")
    print(f"{'='*60}\n")

    if not TRANSKRIPTOR_API_KEY:
        raise SystemExit("❌ TRANSKRIPTOR_API_KEY mangler!")

    state = load_state()
    save_state(state)  # persistér evt. format-migrering med det samme
    resume_pending(state)

    new_videos_found = 0
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

            orders_placed += 1
            if process_video(state, video_id, category_name):
                new_videos_found += 1
            time.sleep(2)
        else:
            continue
        break  # loftet er nået — stop også ydre løkke

    print(f"\n{'='*60}")
    print(f"✅ Tjek komplet - {new_videos_found} nye videoer transskriberet")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
