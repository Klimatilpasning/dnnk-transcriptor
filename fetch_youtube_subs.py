#!/usr/bin/env python3
"""
Hent danske auto-undertekster fra YouTube og skriv dem i DNNK's
transskriptionsformat (tidsstempel HH:MM:SS på egen linje + tekstblok).

Baggrund: Transkriptors YouTube-URL-transskription begyndte at fejle i
juli 2026 (alle ordrer "mislykkedes" — YouTube blokerer sandsynligvis
datacenter-IP'er for lyd-download). YouTube genererer selv danske
auto-undertekster af samme ASR-kvalitet, som kan hentes GRATIS og uden
at bruge Transkriptor-minutter.

Brug:
  python fetch_youtube_subs.py                  # alle 'pending'/'failed' i state
  python fetch_youtube_subs.py VIDEOID [...]    # bestemte videoer
  python fetch_youtube_subs.py --dry-run        # vis hvad der ville blive hentet

BEMÆRK: YouTube kan afvise datacenter-IP'er ("Sign in to confirm you're
not a bot"). Virker det ikke i GitHub Actions, så kør scriptet lokalt.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TRANSCRIPTIONS_FOLDER = Path("transcriptions")
STATE_FILE = "processed_videos.json"
BLOCK_SECONDS = 30          # samme granularitet som Transkriptor-formatet
SUB_LANGS = "da-orig,da"    # dansk original, ellers dansk


# ── VTT-parsing ───────────────────────────────────────────────────────────────

TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->")
INLINE_TAG_RE = re.compile(r"<[^>]+>")


def _clean(line: str) -> str:
    return INLINE_TAG_RE.sub("", line).strip()


def parse_vtt(vtt_text: str) -> list[tuple[int, str]]:
    """VTT → liste af (sekunder, tekstlinje).

    YouTube-auto-undertekster er 'rolling': hver cue gentager forrige linje
    plus et par nye ord. Vi tager derfor kun den SIDSTE tekstlinje i hver
    cue (den nyeste) og springer gentagelser over.
    """
    cues: list[tuple[int, str]] = []
    cur_sec: int | None = None
    cur_lines: list[str] = []

    def flush():
        if cur_sec is None:
            return
        # nyeste indhold = sidste ikke-tomme linje i cue'en
        for raw in reversed(cur_lines):
            txt = _clean(raw)
            if txt:
                cues.append((cur_sec, txt))
                return

    for raw in vtt_text.splitlines():
        m = TS_RE.match(raw.strip())
        if m:
            flush()
            h, mi, s = (int(x) for x in m.groups())
            cur_sec = h * 3600 + mi * 60 + s
            cur_lines = []
        elif cur_sec is not None:
            cur_lines.append(raw)
    flush()

    # Fjern gentagelser: behold kun linjer der tilføjer nyt indhold
    ud: list[tuple[int, str]] = []
    for sec, txt in cues:
        if ud and (txt == ud[-1][1] or ud[-1][1].endswith(txt)):
            continue
        if ud and txt.startswith(ud[-1][1]):
            # cue'en er forrige linje + nye ord → erstat med den længste
            ud[-1] = (ud[-1][0], txt)
            continue
        ud.append((sec, txt))
    return ud


def to_dnnk_format(cues: list[tuple[int, str]], block_seconds: int = BLOCK_SECONDS) -> str:
    """Grupper cues i blokke med tidsstempel HH:MM:SS på egen linje."""
    if not cues:
        return ""
    blokke: list[tuple[int, list[str]]] = []
    blok_start = cues[0][0] // block_seconds * block_seconds
    buffer: list[str] = []
    for sec, txt in cues:
        if sec >= blok_start + block_seconds and buffer:
            blokke.append((blok_start, buffer))
            blok_start = sec // block_seconds * block_seconds
            buffer = []
        buffer.append(txt)
    if buffer:
        blokke.append((blok_start, buffer))

    dele = []
    for start, linjer in blokke:
        ts = f"{start // 3600:02d}:{start % 3600 // 60:02d}:{start % 60:02d}"
        tekst = " ".join(linjer)
        tekst = re.sub(r"\s+", " ", tekst).strip()
        dele.append(f"{ts}\n{tekst}\n")
    return "\n".join(dele)


# ── YouTube ───────────────────────────────────────────────────────────────────

def fetch_subs(video_id: str) -> str | None:
    """Hent danske auto-undertekster som VTT-tekst. None hvis de ikke findes."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "sub")
        cmd = [sys.executable, "-m", "yt_dlp", "--write-auto-subs",
               "--sub-langs", SUB_LANGS, "--sub-format", "vtt",
               "--skip-download", "--no-warnings", "-o", out,
               f"https://youtube.com/watch?v={video_id}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print("      timeout ved hentning")
            return None
        if "Sign in to confirm" in (r.stdout + r.stderr):
            print("      ❌ YouTube kræver login (datacenter-IP blokeret) — kør lokalt")
            return None
        # foretræk da-orig, ellers da
        for suffix in (".da-orig.vtt", ".da.vtt"):
            p = Path(tmp) / f"sub{suffix}"
            if p.exists():
                return p.read_text(encoding="utf-8", errors="replace")
        print("      ingen danske undertekster tilgængelige")
        return None


def save(video_id: str, tekst: str, category: str) -> Path:
    TRANSCRIPTIONS_FOLDER.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPTIONS_FOLDER / f"{category}_{video_id}_{stamp}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("=== DNNK Webinar Transskription ===\n")
        f.write(f"Kategori: {category}\n")
        f.write(f"Video ID: {video_id}\n")
        f.write(f"URL: https://youtube.com/watch?v={video_id}\n")
        f.write(f"Kilde: YouTube auto-undertekster (dansk)\n")
        f.write(f"Transskriberet: {datetime.now().isoformat()}\n")
        f.write(f"\n{'='*50}\n\n")
        f.write(tekst)
    return path


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv

    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)

    if argv:
        ids = argv
    else:
        ids = [v for v, e in state.items()
               if e.get("status") in ("pending", "failed")]

    print(f"{len(ids)} videoer skal hentes via YouTube-undertekster")
    if dry:
        for v in ids:
            print(f"  {v}  (status: {state.get(v, {}).get('status')})")
        return

    ok = mangler = 0
    for i, vid in enumerate(ids, 1):
        entry = state.get(vid, {})
        cat = entry.get("category") or "Oevrige"
        print(f"[{i}/{len(ids)}] {vid} ({cat})")
        vtt = fetch_subs(vid)
        if not vtt:
            mangler += 1
            continue
        tekst = to_dnnk_format(parse_vtt(vtt))
        if len(tekst) < 500:
            print(f"      for lidt tekst ({len(tekst)} tegn) — springes over")
            mangler += 1
            continue
        p = save(vid, tekst, cat)
        state[vid] = {**entry, "status": "done", "kilde": "youtube-subs",
                      "order_id": None,
                      "last_attempt": datetime.now().isoformat(timespec="seconds")}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"      ✅ {p.name} ({len(tekst)} tegn)")
        ok += 1

    print(f"\nFærdig: {ok} hentet, {mangler} uden brugbare undertekster")


if __name__ == "__main__":
    main()
