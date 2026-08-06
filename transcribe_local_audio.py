#!/usr/bin/env python3
"""
Transskribér webinarer LOKALT fra lyd — for videoer uden YouTube-undertekster.

Baggrund: Transkriptors YouTube-transskription fejler (YouTube blokerer
datacenter-IP'er), og nogle videoer har ingen danske auto-undertekster.
Denne vej henter lyden med yt-dlp (fra denne maskines almindelige
forbindelse) og transskriberer med faster-whisper — gratis og uden
Transkriptor-minutter.

Brug:
  python transcribe_local_audio.py VIDEOID [VIDEOID ...]
  python transcribe_local_audio.py --failed          # alle 'failed' i state
  python transcribe_local_audio.py --failed --min-min 20   # kun >= 20 min

Env:
  WHISPER_MODEL   small (default) | medium | large-v3
  WHISPER_THREADS antal CPU-tråde (default: cpu_count-4)

Kræver: pip install faster-whisper yt-dlp
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TRANSCRIPTIONS_FOLDER = Path("transcriptions")
STATE_FILE = "processed_videos.json"
BLOCK_SECONDS = 30
MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
THREADS = int(os.environ.get("WHISPER_THREADS", max(4, (os.cpu_count() or 8) - 4)))


def hent_lyd(video_id: str, mappe: str) -> Path | None:
    """Hent lyd-only stream. Ingen ffmpeg-postprocessing (whisper læser selv)."""
    out = os.path.join(mappe, f"{video_id}.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio[abr<=128]/bestaudio",
           "--no-warnings", "--no-playlist", "-o", out,
           f"https://youtube.com/watch?v={video_id}"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    if "Sign in to confirm" in (r.stdout + r.stderr):
        print("      ❌ YouTube blokerer denne IP — kør fra almindelig forbindelse")
        return None
    filer = sorted(Path(mappe).glob(f"{video_id}.*"))
    return filer[0] if filer else None


def til_dnnk_format(segmenter, block_seconds: int = BLOCK_SECONDS) -> str:
    """Whisper-segmenter → tidsstempel-blokke som resten af korpusset."""
    blokke: dict[int, list[str]] = {}
    for s in segmenter:
        blok = int(s.start) // block_seconds * block_seconds
        blokke.setdefault(blok, []).append(s.text.strip())
    dele = []
    for start in sorted(blokke):
        ts = f"{start // 3600:02d}:{start % 3600 // 60:02d}:{start % 60:02d}"
        dele.append(f"{ts}\n{' '.join(blokke[start])}\n")
    return "\n".join(dele)


def gem(video_id: str, tekst: str, category: str, model: str) -> Path:
    TRANSCRIPTIONS_FOLDER.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = TRANSCRIPTIONS_FOLDER / f"{category}_{video_id}_{stamp}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("=== DNNK Webinar Transskription ===\n")
        f.write(f"Kategori: {category}\n")
        f.write(f"Video ID: {video_id}\n")
        f.write(f"URL: https://youtube.com/watch?v={video_id}\n")
        f.write(f"Kilde: lokal transskription (faster-whisper {model}, dansk)\n")
        f.write(f"Transskriberet: {datetime.now().isoformat()}\n")
        f.write(f"\n{'='*50}\n\n")
        f.write(tekst)
    return path


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    min_min = 0
    if "--min-min" in sys.argv:
        min_min = int(sys.argv[sys.argv.index("--min-min") + 1])

    state = json.load(open(STATE_FILE, encoding="utf-8"))
    if "--failed" in sys.argv:
        ids = [v for v, e in state.items() if e.get("status") == "failed"]
    else:
        ids = argv
    if not ids:
        print("Ingen videoer angivet (brug VIDEOID eller --failed)")
        return

    from faster_whisper import WhisperModel
    print(f"Indlæser whisper-model '{MODEL_NAME}' ({THREADS} tråde) …")
    model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8",
                         cpu_threads=THREADS)
    print("model klar\n")

    ok = sprunget = 0
    for i, vid in enumerate(ids, 1):
        entry = state.get(vid, {})
        cat = entry.get("category") or "Oevrige"
        print(f"[{i}/{len(ids)}] {vid} ({cat})")
        with tempfile.TemporaryDirectory() as tmp:
            lyd = hent_lyd(vid, tmp)
            if not lyd:
                print("      kunne ikke hente lyd — springes over")
                sprunget += 1
                continue
            segs, info = model.transcribe(str(lyd), language="da", vad_filter=True)
            if min_min and info.duration < min_min * 60:
                print(f"      {info.duration/60:.0f} min < {min_min} min — springes over")
                sprunget += 1
                continue
            print(f"      transskriberer {info.duration/60:.0f} min lyd …")
            tekst = til_dnnk_format(list(segs))
        if len(tekst) < 500:
            print(f"      for lidt tekst ({len(tekst)} tegn) — springes over")
            sprunget += 1
            continue
        p = gem(vid, tekst, cat, MODEL_NAME)
        state[vid] = {**entry, "status": "done", "order_id": None,
                      "kilde": f"whisper-{MODEL_NAME}", "attempts": 0,
                      "last_attempt": datetime.now().isoformat(timespec="seconds")}
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        print(f"      ✅ {p.name} ({len(tekst)} tegn)")
        ok += 1

    print(f"\nFærdig: {ok} transskriberet, {sprunget} sprunget over")


if __name__ == "__main__":
    main()
