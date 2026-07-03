#!/usr/bin/env python3
"""
Konservativ AI-oprensning af transskriptioner.

Modellen må KUN udpege rettelsespar (hørefejl i navne/fagtermer) —
selve erstatningen sker deterministisk her i scriptet med ord-grænser,
så intet omformuleres. Alle ændringer logges i cleanup_log.json og
kørslen kan genoptages (cleanup_state.json).

Kører via DNNK's chat-proxy (server-side API-nøgle):
  python cleanup_transcripts.py [--pilot N]
Env:  DNNK_ACCESS_CODE (påkrævet), DNNK_PROXY_URL (default Render-URL)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

PROXY_URL = os.environ.get("DNNK_PROXY_URL", "https://dnnk-klimamonitor-proxy.onrender.com")
ACCESS_CODE = os.environ.get("DNNK_ACCESS_CODE")
FOLDER = Path("transcriptions")
STATE_FILE = Path("cleanup_state.json")
LOG_FILE = Path("cleanup_log.json")

PACE_SECONDS = 3.5        # proxy tillader 20 kald/min
MAX_CHARS = 90_000        # proxy afviser payloads > 100k tegn
MAX_PAIRS = 40

# Kendte gengangere — anvendes altid, også hvis AI'en overser dem.
SEED_GLOSSARY = [
    ("DNAK", "DNNK"),
    ("DN AK", "DNNK"),
    ("tiktok", "Tech Talk"),
    ("tik tok", "Tech Talk"),
    ("tektop", "Tech Talk"),
    ("teksttalk", "Tech Talk"),
    ("tekst talk", "Tech Talk"),
]

SYSTEM_PROMPT = """Du får en dansk transskription af et DNNK-webinar om klimatilpasning. Talegenkendelsen har hørefejl i navne og fagtermer.

Returnér KUN et JSON-array af rettelsespar: [{"forkert": "...", "rettet": "..."}]

Find åbenlyse fejlhøringer af:
- organisationsnavne (fx "DNAK" → "DNNK", "DHI", "HOFOR", "Novafos", kommunenavne)
- webinarformater ("tiktok"/"tektop"/"teksttalk" når der menes "Tech Talk")
- person- og virksomhedsnavne, produkt-/projektnavne (fx "futhus city flow" → "Future City Flow")
- fagtermer (fx "svilling" → "tvilling" i "digital tvilling", "LAR", "BNBO", "skybrudssikring")

REGLER:
- Kun rettelser du er HELT sikker på ud fra konteksten.
- "forkert" skal være den PRÆCISE tekststreng som den står i transskriptet (bevar store/små bogstaver).
- Ret ALDRIG almindelige danske ord, grammatik, ordstilling eller formuleringer.
- "forkert" skal være mindst 3 tegn og må ikke være et almindeligt dansk ord.
- Max 40 par. Returnér [] hvis intet skal rettes.
- Svar KUN med JSON-arrayet, ingen forklaring."""


INDEX_PATH = Path(os.environ.get(
    "SEARCH_INDEX",
    str(Path.home() / "Desktop/DNNK/audit/dnnk-vidensassistent/search-index.json"),
))


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def build_evidence():
    """Tekstmasse af kendte navne/termer fra søgeindekset — bruges til at
    validere AI'ens rettelsesforslag, så den ikke kan opfinde nye navne."""
    parts = ["DNNK", "Tech Talk", "Godmorgen med DNNK", "Masterclass", "LAR",
             "BNBO", "DK2020", "digital tvilling", "klimatilpasning"]
    for e in load_json(INDEX_PATH, []):
        parts.append(e.get("title") or "")
        parts.extend(e.get("keywords") or [])
        parts.extend(e.get("places") or [])
        for s in e.get("speakers") or []:
            parts.append(s.get("name") or "")
            parts.append(s.get("org") or "")
    return " ".join(parts).lower()


def _lev(a, b, cap=3):
    """Levenshtein-afstand med loft (nok til 'lille fonetisk rettelse')."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def pair_is_safe(wrong, right, evidence):
    """Acceptér kun rettelser med evidens eller lille fonetisk afstand.
    Afviser store spring som 'Sydlinus'->'3B' og 'Pia'->'Pierre'."""
    if right.lower() in evidence:
        return True, "evidens"
    ww, rw = wrong.split(), right.split()
    if len(ww) == len(rw) and all(_lev(a.lower(), b.lower(), 2) <= 2 for a, b in zip(ww, rw)):
        return True, "fonetisk"
    return False, "afvist"


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def ask_model(text):
    """Returnerer liste af (forkert, rettet)-par fra modellen."""
    resp = requests.post(
        f"{PROXY_URL}/chat",
        headers={"Content-Type": "application/json", "X-DNNK-Code": ACCESS_CODE},
        json={
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": text[:MAX_CHARS]}],
            "max_tokens": 2000,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    blocks = data.get("content") or []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
    if not texts:
        raise RuntimeError(f"Uventet svar: {str(data)[:200]}")
    raw = "\n".join(texts).strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    pairs = json.loads(m.group(0))
    out = []
    for p in pairs[:MAX_PAIRS]:
        f, r = (p.get("forkert") or "").strip(), (p.get("rettet") or "").strip()
        if len(f) >= 3 and r and f.lower() != r.lower():
            out.append((f, r))
    return out


def apply_pairs(text, pairs):
    """Deterministisk erstatning med ord-grænser. Returnerer (ny_tekst, anvendte)."""
    applied = []
    for wrong, right in pairs:
        pattern = re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)")
        new_text, n = pattern.subn(right, text)
        if n:
            applied.append({"forkert": wrong, "rettet": right, "antal": n})
            text = new_text
    return text, applied


def main():
    if not ACCESS_CODE:
        raise SystemExit("DNNK_ACCESS_CODE mangler i miljøet")

    pilot = None
    if "--pilot" in sys.argv:
        pilot = int(sys.argv[sys.argv.index("--pilot") + 1])

    state = load_json(STATE_FILE, {})
    log = load_json(LOG_FILE, {})
    evidence = build_evidence()
    print(f"evidens-tekstmasse: {len(evidence)} tegn fra søgeindekset")

    files = sorted(p for p in FOLDER.rglob("*.txt") if not p.name.startswith("PDF_"))
    todo = [p for p in files if str(p) not in state]
    if pilot:
        todo = todo[:pilot]
    print(f"{len(files)} transskriptioner, {len(todo)} skal behandles")

    for i, path in enumerate(todo, 1):
        rel = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")

        try:
            ai_pairs = ask_model(text)
        except Exception as e:
            print(f"[{i}/{len(todo)}] FEJL {path.name[:50]}: {e} — springes over, prøves næste kørsel")
            time.sleep(PACE_SECONDS)
            continue

        safe_pairs, rejected = [], []
        for wrong, right in ai_pairs:
            ok, reason = pair_is_safe(wrong, right, evidence)
            (safe_pairs if ok else rejected).append(
                {"forkert": wrong, "rettet": right, "grund": reason})

        new_text, applied = apply_pairs(
            text, SEED_GLOSSARY + [(p["forkert"], p["rettet"]) for p in safe_pairs])
        if applied or rejected:
            path.write_text(new_text, encoding="utf-8") if applied else None
            log[rel] = {"anvendt": applied, "afvist": rejected}
            save_json(LOG_FILE, log)
        state[rel] = {"pairs": len(applied), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        save_json(STATE_FILE, state)

        total = sum(a["antal"] for a in applied)
        print(f"[{i}/{len(todo)}] {path.name[:50]:50s} {len(applied)} anvendt ({total} erst.), {len(rejected)} afvist")
        time.sleep(PACE_SECONDS)

    print("\nFærdig. Se cleanup_log.json for alle ændringer.")


if __name__ == "__main__":
    main()
