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
    "Fremtidsvaerksted":    "https://www.dnnk.dk/category/arrangementer/fremtid/",
    "Arrangementer":        "https://www.dnnk.dk/arrangementer/",
    "Vidensbank":           "https://www.dnnk.dk/category/vidensbank/",
    "Studieture":           "https://www.dnnk.dk/online-studietur/",
    "VIP":                  "https://www.dnnk.dk/vip-vand-innovation-pitch-2/",
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


MAX_SUBPAGES = 120  # undersider pr. kategori (Masterclass har én side pr. event)
#
# Hævet fra 20: undersiderne sorteres alfabetisk, og WordPress' datoarkiver
# (/2022/09/05/) sorterer før alle bogstav-URL'er. På Konferencer gik 18 af
# de 20 pladser til datoarkiver uden en eneste video, mens de navngivne
# temadags- og konferencesider lå på plads 24-51 og aldrig blev hentet.
# Målt: Konferencer gav 2 videoer med loft 20, 68 med loftet hævet.
# Højeste reelle behov er i dag 51 undersider; 120 giver margin.

# Datoarkiver (/2025/01/16/) indeholder kun links til indlæg, aldrig
# YouTube-embeds. De optog 52 af 197 hentninger pr. kørsel til ingen nytte.
DATE_ARCHIVE_RE = re.compile(r'/\d{4}/\d{2}(/\d{2})?/?$')


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
                    and '/page/' not in href
                    and not DATE_ARCHIVE_RE.search(href)):
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

WP_API = "https://www.dnnk.dk/wp-json"


def _wp_alle(endpoint, felter):
    """Paginér gennem et WordPress REST-endpoint til det løber tørt."""
    ud, side = [], 1
    headers = {"User-Agent": "Mozilla/5.0"}
    while side <= 30:
        try:
            r = requests.get(f"{WP_API}/{endpoint}",
                             params={"per_page": 100, "page": side,
                                     "_fields": felter},
                             timeout=40, headers=headers)
        except requests.RequestException as e:
            print(f"   ⚠️  {endpoint} side {side}: {e}")
            break
        if r.status_code != 200:
            break            # 400 = ingen flere sider; det er den normale exit
        try:
            d = r.json()
        except ValueError:
            break
        if not isinstance(d, list) or not d:
            break
        ud.extend(d)
        side += 1
    return ud


def discover_via_rest():
    """Alle YouTube-ID'er der er linket fra dnnk.dk, uanset hvor de står.

    Kategorisidecrawlet kan kun nå sider der er linket fra én af de 11
    kategorisider, og kun de første MAX_SUBPAGES af dem. Sider som
    /digitale-vaerktoejer-til-klimatilpasning-risikokortlaegning/ er ikke
    linket fra nogen kategoriside og var derfor uopnåelige uanset loft.

    WordPress' eget REST API kender hvert indlæg, hver side og hvert event,
    så det er en fuldstændig kilde. Returnerer {video_id: kildesidens_url}.
    """
    fundet = {}
    for navn, endpoint in (("indlæg", "wp/v2/posts"), ("sider", "wp/v2/pages")):
        poster = _wp_alle(endpoint, "link,content")
        n = 0
        for p in poster:
            html = (p.get("content") or {}).get("rendered", "")
            for vid in set(YOUTUBE_ID_RE.findall(html)):
                fundet.setdefault(vid, p.get("link", ""))
                n += 1
        print(f"   REST {navn}: {len(poster)} poster, {n} video-referencer")

    # The Events Calendar har sit eget endpoint og indgår ikke i wp/v2
    side, events = 1, []
    while side <= 20:
        try:
            r = requests.get(f"{WP_API}/tribe/events/v1/events",
                             params={"per_page": 50, "page": side}, timeout=40,
                             headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        d = r.json().get("events", [])
        if not d:
            break
        events.extend(d)
        side += 1
    for e in events:
        html = (e.get("description") or "") + " " + (e.get("url") or "")
        for vid in set(YOUTUBE_ID_RE.findall(html)):
            fundet.setdefault(vid, e.get("url", ""))
    print(f"   REST events: {len(events)} poster")
    return fundet


def discover_all_videos():
    """Samlet opdagelse: kategorikravl først (giver kategorinavnet, som
    bruges i filnavnet), derefter REST som sikkerhedsnet for alt det
    crawlet ikke kan se. Returnerer {video_id: kategorinavn}."""
    fundet = {}
    for navn, url in CATEGORIES.items():
        print(f"\n📂 Tjekker kategori: {navn}")
        ids = scrape_category_for_videos(url)
        print(f"   Fandt {len(ids)} videoer i alt")
        for vid in ids:
            fundet.setdefault(vid, navn)

    print("\n🌐 Supplerer via WordPress REST API")
    for vid, kilde in discover_via_rest().items():
        if vid not in fundet:
            # Ingen kategoriside kender den; udled et navn af kildesiden,
            # så filnavnet stadig siger noget om hvor videoen hører hjemme.
            fundet[vid] = _kategori_af_url(kilde)
    return fundet


def _kategori_af_url(url):
    """Gæt en kategori ud fra kildesidens slug — kun til filnavnet."""
    u = (url or "").lower()
    for noegle, navn in (
            ("tech-talk", "Tech_Talks"),
            ("god-morgen", "Godmorgen_med_DNNK"),
            ("godmorgen", "Godmorgen_med_DNNK"),
            ("masterclass", "DNNK_Masterclass"),
            ("jura", "Jura"),
            ("fremtidsvaerksted", "Fremtidsvaerksted"),
            ("vip", "VIP"),
            ("studietur", "Studieture"),
            ("temadag", "Konferencer"),
            ("konference", "Konferencer"),
            ("aarsmoede", "Konferencer"),
            ("summit", "Konferencer"),
            ("workshop", "Konferencer"),
            ("webinar", "Oevrige")):
        if noegle in u:
            return navn
    return "Oevrige"


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
        if response.status_code in (401, 403):
            raise AuthError(f"HTTP {response.status_code} ved ordreafgivelse")
        response.raise_for_status()
        order_id = response.json().get('order_id')
        if not order_id:
            print(f"❌ Intet order_id i svar: {response.json()}")
        return order_id
    except AuthError:
        raise
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

def hent_undertekster(video_id):
    """YouTube's danske auto-undertekster i DNNK-format, eller None.
    Primær transskriptionsvej: gratis og uden Transkriptor-minutter.
    Se fetch_youtube_subs.py for detaljer og manuel batch-kørsel."""
    try:
        from fetch_youtube_subs import (fetch_subs, parse_vtt, to_dnnk_format,
                                        TransientSubsError)
    except ImportError:
        return None
    try:
        vtt = fetch_subs(video_id)
        if not vtt:
            return None
        tekst = to_dnnk_format(parse_vtt(vtt))
        return tekst if len(tekst) >= 500 else None
    except TransientSubsError:
        raise          # midlertidig — main() må ikke tælle den som en fejl
    except Exception as e:
        print(f"      ⚠️ Undertekst-hentning fejlede ({e})")
        return None


try:
    from fetch_youtube_subs import TransientSubsError
except ImportError:      # scriptet kan køre uden hjælpemodulet
    class TransientSubsError(Exception):
        pass


class AuthError(Exception):
    """API'et afviser vores nøgle (401/403) — permanent fejl, ikke midlertidig.
    Typiske årsager: udløbet/roteret API-nøgle, eller opbrugt minut-kvote."""


def check_order(order_id):
    """Ét status-tjek uden ventetid.
    Returnerer ('completed', tekst) / ('failed', None) / ('working', None).
    Kaster AuthError ved 401/403, så kørslen kan stoppe med det samme
    i stedet for at polle i timevis mod et API der afviser os."""
    status_url = f"https://api.tor.app/developer/transcription/{order_id}"
    try:
        resp = requests.get(status_url, headers=api_headers(), timeout=30)
        if resp.status_code in (401, 403):
            # Log API'ets egen fejlbesked (afslører om det er ugyldig nøgle,
            # manglende API-abonnement eller opbrugt kvote). Nøglen logges ALDRIG.
            raise AuthError(f"HTTP {resp.status_code} fra Transkriptor. "
                            f"API-svar: {resp.text[:300]!r}")
        resp.raise_for_status()
        status = resp.json().get('status', '').lower()
    except AuthError:
        raise
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
            try:
                status, text = check_order(entry["order_id"])
            except AuthError as e:
                # Permanent auth-fejl: stop straks. Ordrerne forbliver pending
                # og hentes når adgangen virker igen — der betales ikke igen.
                raise AuthError(
                    f"{e} — {len(pending)} betalte ordrer kan ikke hentes. "
                    "Tjek TRANSKRIPTOR_API_KEY og minut-kvoten på Transkriptor-kontoen."
                ) from None
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
    transient = 0
    alle_videoer = discover_all_videos()
    print(f"\n🔎 {len(alle_videoer)} videoer opdaget i alt\n")

    for video_id, category_name in alle_videoer.items():
        entry = state.get(video_id)
        if entry:
            if entry.get("status") in ("done", "pending"):
                continue
            if entry.get("attempts", 0) >= MAX_ATTEMPTS:
                continue  # opgivet — undgå at betale for samme fejl hver dag

        if orders_placed >= MAX_NEW_PER_RUN:
            print(f"   ⏸️ Loft på {MAX_NEW_PER_RUN} nye ordrer nået — resten tages næste kørsel")
            break

        print(f"   🆕 Ny video: {video_id}")
        # 1a) FØRST: YouTube's egne danske auto-undertekster — gratis og
        #     hurtigt. Transkriptors YouTube-transskription fejlede helt
        #     i juli 2026, så undertekster er nu den primære vej.
        try:
            tekst = hent_undertekster(video_id)
        except TransientSubsError as e:
            # Rate-limit eller IP-blokering: vi fik aldrig et svar på om
            # videoen har undertekster. Lad state være urørt, så attempts
            # ikke tælles op og videoen ikke opgives permanent.
            print(f"      ⏳ midlertidig hindring ({e}) — prøves igen næste kørsel")
            transient += 1
            continue
        if tekst:
            save_transcription(video_id, tekst, category_name)
            mark(state, video_id, "done", order_id=None, kilde="youtube-subs")
            print("      ✅ hentet via YouTube-undertekster (0 min forbrugt)")
            continue

        # 1b) Ellers: Transkriptor-ordre (koster minutter af kvoten).
        #     SLÅET FRA som default: Transkriptors YouTube-transskription
        #     fejlede 100%% i juli 2026 (YouTube blokerer datacenter-IP'er),
        #     så ordrer ville kun brænde kvote. Sæt TRANSKRIPTOR_YOUTUBE=true
        #     for at prøve igen, hvis de får det til at virke.
        if os.environ.get("TRANSKRIPTOR_YOUTUBE", "").lower() not in ("1", "true", "yes"):
            print("      ingen undertekster — Transkriptor-fallback er slået fra")
            mark(state, video_id, "failed",
                 note="ingen danske YouTube-undertekster; kræver lyd-transskription")
            continue
        print("      ingen undertekster — afgiver Transkriptor-ordre...")
        order_id = start_order(f"https://youtube.com/watch?v={video_id}")
        if order_id:
            mark(state, video_id, "pending", order_id=order_id, category=category_name)
            orders_placed += 1
        else:
            mark(state, video_id, "failed")
        time.sleep(2)

    # 2) Saml alle færdige transskriptioner ind (også fra tidligere kørsler)
    collect_pending(state)

    done_now = sum(1 for e in state.values() if e.get("status") == "done")
    print(f"\n{'='*60}")
    print(f"✅ Kørsel slut — {orders_placed} nye ordrer afgivet, {done_now} videoer færdige i alt")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    try:
        main()
    except AuthError as e:
        # Fejl HURTIGT og TYDELIGT ved auth-problemer i stedet for at polle
        # i timevis mod et API der afviser os (skete 1/7–6/8 2026: 403 i
        # 5.100 forsøg pr. kørsel = 4 timer spildt dagligt uden resultat).
        print(f"\n::error::Transkriptor afviser adgang: {e}")
        print("HANDLING: tjek (1) at TRANSKRIPTOR_API_KEY-secret er gyldig, og "
              "(2) at der er minutter tilbage på Transkriptor-abonnementet.")
        print("Afventende ordrer er BEVARET som 'pending' og hentes automatisk, "
              "når adgangen virker igen — der betales ikke igen.")
        raise SystemExit(1)
