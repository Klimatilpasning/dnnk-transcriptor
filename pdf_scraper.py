#!/usr/bin/env python3
"""
DNNK PDF-scraper
Scanner DNNK's hjemmeside for PDF-links, downloader og udtrækker tekst
Gemmer som .txt filer i transcriptions-mappen
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path
from pypdf import PdfReader
import io

TRANSCRIPTIONS_FOLDER = Path("transcriptions")
PROCESSED_PDFS_FILE = "processed_pdfs.json"

# Sider der scannes for PDF-links
PDF_PAGES = {
    "Vidensbank":       "https://www.dnnk.dk/category/vidensbank/",
    "Arrangementer":    "https://www.dnnk.dk/arrangementer/",
    "Konferencer":      "https://www.dnnk.dk/optagelser-fra-konferencer-og-temadage/",
    "Jura":             "https://www.dnnk.dk/jura-i-klimatilpasning/",
    "DNNK_Masterclass": "https://www.dnnk.dk/dnnk-masterclass/",
    "Tech_Talks":       "https://www.dnnk.dk/tech-talks/",
    "Godmorgen":        "https://www.dnnk.dk/god-morgen-med-dnnk/",
}

def load_processed_pdfs():
    if os.path.exists(PROCESSED_PDFS_FILE):
        with open(PROCESSED_PDFS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_processed_pdf(pdf_url):
    processed = load_processed_pdfs()
    if pdf_url not in processed:
        processed.append(pdf_url)
        with open(PROCESSED_PDFS_FILE, 'w', encoding='utf-8') as f:
            json.dump(processed, f, indent=2, ensure_ascii=False)

def scrape_page_for_pdfs(page_url):
    """Find alle PDF-links på en side og undersider"""
    try:
        headers = {"User-Agent": "DNNK-PDFScraper/1.0"}
        resp = requests.get(page_url, timeout=30, headers=headers)
        soup = BeautifulSoup(resp.content, 'html.parser')
        
        pdf_links = []
        
        # Find direkte PDF-links
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.lower().endswith('.pdf'):
                # Gør URL absolut
                if href.startswith('http'):
                    pdf_links.append((href, link.get_text(strip=True) or href))
                elif href.startswith('/'):
                    pdf_links.append((f"https://www.dnnk.dk{href}", link.get_text(strip=True) or href))
        
        # Find links til undersider der kan indeholde PDFs
        post_links = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'dnnk.dk' in href and not href.endswith('.pdf') and not '#' in href:
                if href not in [page_url]:
                    post_links.append(href)
        
        # Scan de første 20 undersider (sorteret så udvalget er deterministisk)
        for post_url in sorted(set(post_links))[:20]:
            try:
                sub_resp = requests.get(post_url, timeout=15, headers=headers)
                sub_soup = BeautifulSoup(sub_resp.content, 'html.parser')
                for link in sub_soup.find_all('a', href=True):
                    href = link['href']
                    if href.lower().endswith('.pdf'):
                        if href.startswith('http'):
                            pdf_links.append((href, link.get_text(strip=True) or href))
                        elif href.startswith('/'):
                            pdf_links.append((f"https://www.dnnk.dk{href}", link.get_text(strip=True) or href))
            except requests.RequestException as e:
                print(f"   ⚠️ Kunne ikke hente underside {post_url}: {e}")
        
        # Fjern dubletter
        seen = set()
        unique = []
        for url, title in pdf_links:
            if url not in seen:
                seen.add(url)
                unique.append((url, title))
        
        return unique
    
    except Exception as e:
        print(f"❌ Fejl ved scraping af {page_url}: {e}")
        return []

def extract_text_from_pdf(pdf_url):
    """Download og udtræk tekst fra PDF.
    Returnerer (tekst, permanent_fejl): tekst=None + permanent=False betyder
    forbigående fejl (netværk/server) — PDF'en skal IKKE markeres som behandlet."""
    try:
        headers = {"User-Agent": "DNNK-PDFScraper/1.0"}
        resp = requests.get(pdf_url, timeout=60, headers=headers)

        if resp.status_code != 200:
            print(f"   ❌ HTTP {resp.status_code}")
            # 404/410 = permanent væk; 5xx/429 = forbigående
            return None, resp.status_code in (404, 410, 403)

        if len(resp.content) < 1000:
            print(f"   ❌ For lille fil ({len(resp.content)} bytes)")
            return None, True

        reader = PdfReader(io.BytesIO(resp.content))

        if len(reader.pages) == 0:
            return None, True

        text = ""
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n\n"

        if len(text.strip()) < 100:
            print(f"   ⚠️ Meget lidt tekst udtrukket ({len(text)} tegn) — muligvis scannet PDF")
            return None, True

        return text.strip(), False

    except requests.RequestException as e:
        print(f"   ❌ Netværksfejl (prøves igen næste kørsel): {e}")
        return None, False
    except Exception as e:
        print(f"   ❌ Fejl ved PDF-parsning: {e}")
        return None, True

def safe_filename(title, url):
    """Lav et sikkert filnavn fra titel eller URL"""
    if title and len(title) > 5:
        name = re.sub(r'[^\w\s-]', '', title)
        name = re.sub(r'\s+', '_', name.strip())
        name = name[:80]
    else:
        # Brug URL hash
        name = hashlib.md5(url.encode()).hexdigest()[:12]
    return f"PDF_{name}"

def save_pdf_text(title, url, text, category):
    """Gem PDF-tekst som .txt fil"""
    TRANSCRIPTIONS_FOLDER.mkdir(exist_ok=True)
    filename_base = safe_filename(title, url)
    filename = TRANSCRIPTIONS_FOLDER / f"{filename_base}.txt"
    
    # Undgå duplikater
    counter = 1
    while filename.exists():
        filename = TRANSCRIPTIONS_FOLDER / f"{filename_base}_{counter}.txt"
        counter += 1
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"=== DNNK PDF Dokument ===\n")
        f.write(f"Titel: {title}\n")
        f.write(f"Kategori: {category}\n")
        f.write(f"Kilde: {url}\n")
        f.write(f"Indekseret: {datetime.now().isoformat()}\n")
        f.write(f"\n{'='*50}\n\n")
        f.write(text)
    
    print(f"   ✅ Gemt: {filename.name}")
    return filename

def main():
    print(f"\n{'='*60}")
    print(f"📄 DNNK PDF-scraper - {datetime.now()}")
    print(f"{'='*60}\n")
    
    processed_pdfs = load_processed_pdfs()
    new_pdfs = 0
    
    for category, page_url in PDF_PAGES.items():
        print(f"\n📂 Scanner: {category}")
        pdf_links = scrape_page_for_pdfs(page_url)
        print(f"   Fandt {len(pdf_links)} PDF-links")
        
        for pdf_url, title in pdf_links:
            if pdf_url in processed_pdfs:
                continue
            
            print(f"\n   📄 Ny PDF: {title[:60]}")
            print(f"      URL: {pdf_url}")
            
            text, permanent_fejl = extract_text_from_pdf(pdf_url)

            if text:
                save_pdf_text(title, pdf_url, text, category)
                save_processed_pdf(pdf_url)
                new_pdfs += 1
            elif permanent_fejl:
                print(f"   ⚠️ Springer over permanent — ingen tekst at hente")
                save_processed_pdf(pdf_url)
            else:
                print(f"   ⚠️ Forbigående fejl — prøves igen næste kørsel")
    
    print(f"\n{'='*60}")
    print(f"✅ Færdig — {new_pdfs} nye PDF-dokumenter indekseret")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
