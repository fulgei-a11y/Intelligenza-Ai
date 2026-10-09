#!/usr/bin/env python3
"""Crea l'MP3 di un'edizione con la voce italiana Paola (sherpa-onnx, offline), come negli altri progetti.

Uso:  python tools/audio.py [AAAA-MM-GG]      (senza data: l'edizione più recente)
Legge docs/data/<giorno>.json, scrive docs/audio/<giorno>.mp3 e aggiunge al JSON "audio" e "durata".
Cancella gli MP3 più vecchi di GIORNI_AUDIO giorni (default 60), per non riempire lo spazio di GitHub Pages:
le pagine di quei giorni restano, solo senza il pulsante di ascolto.
"""
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
AUDIO = ROOT / "docs" / "audio"
GIORNI_AUDIO = int(os.environ.get("GIORNI_AUDIO", "60"))

SHERPA_VER = "1.12.14"
SHERPA_URL = (f"https://github.com/k2-fsa/sherpa-onnx/releases/download/v{SHERPA_VER}/"
              f"sherpa-onnx-v{SHERPA_VER}-linux-x64-shared.tar.bz2")
VOICE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-it_IT-paola-medium.tar.bz2"
CACHE = os.path.expanduser("~/.cache/ai-news-tts")
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]

# Sigle e parole inglesi che la voce italiana leggerebbe male.
PRONUNCE = [
    (r"\bAI\b", "A I"), (r"\bA\.I\.", "A I"), (r"\bIA\b", "I A"), (r"\bLLM\b", "elle elle emme"),
    (r"\bGPT\b", "G P T"), (r"\bGPU\b", "G P U"), (r"\bAPI\b", "A P I"), (r"\bAGI\b", "A G I"),
    (r"\bUSA\b", "Stati Uniti"), (r"\bUE\b", "Unione europea"), (r"\bOpenAI\b", "Open A I"),
    (r"\bChatGPT\b", "Ciat G P T"), (r"\bDeepMind\b", "Dip Maind"), (r"\bGoogle\b", "Gugol"),
    (r"\bMicrosoft\b", "Maicrosoft"), (r"\bCopilot\b", "Copailot"),
    (r"\bClaude\b", "Clod"), (r"\bAnthropic\b", "Antropic"), (r"\bNvidia\b", "Envidia"),
    (r"\bHugging Face\b", "Haghing Feis"),
    (r"\bchatbot\b", "ciatbot"), (r"\bdeepfake\b", "dipfeik"), (r"\bopen source\b", "open sors"),
]


def normalizza(s):
    s = re.sub(r"\s+", " ", str(s or ""))
    for rx, sost in PRONUNCE:
        s = re.sub(rx, sost, s)
    s = re.sub(r"\$\s?(\d[\d.,]*)\s?(miliardi|milioni|mila)?", lambda m: f"{m[1]} {m[2] or ''} dollari", s)
    s = re.sub(r"(\d[\d.,]*)\s?\$", r"\1 dollari", s)
    s = s.replace("€", " euro").replace("%", " per cento").replace("&", " e ")
    s = re.sub(r"\s[—–-]\s", ", ", s).replace("·", ",").replace("→", ",")
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"(?<!\w)'|'(?!\w)", " ", s)          # virgolette semplici, non gli apostrofi
    s = re.sub(r"[\"«»“”*_#]", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def copione(dati):
    r = dati["riassunto"]
    d = datetime.strptime(dati["giorno"], "%Y-%m-%d")
    pezzi = [f"A I News Digest di {GIORNI[d.weekday()]} {d.day} {MESI[d.month]} {d.year}.",
             normalizza(r.get("titolo_giorno")), normalizza(r.get("in_breve"))]
    if r.get("da_provare"):
        pezzi.append("Strumenti nuovi." if r.get("formato") == 2 else "Da provare oggi.")
        for x in r["da_provare"]:
            pezzi.append(normalizza(f"{x.get('cosa', '')}. {x.get('a_cosa_serve', '')}"))
    for s in r.get("sezioni", []):
        pezzi.append(normalizza(s.get("titolo")))
        for n in s.get("notizie", []):
            pezzi.append(normalizza(n.get("titolo")))
            pezzi.append(normalizza(n.get("riassunto")))
    pezzi.append("Le fonti di ogni notizia sono sul sito. Buona giornata.")
    return [p for p in pezzi if p and p != "."]


def spezza(testo, limite=500):
    parti, buf = [], ""
    for f in re.split(r"(?<=[.!?;])\s+", testo):
        if buf and len(buf) + len(f) + 1 > limite:
            parti.append(buf)
            buf = f
        else:
            buf = (buf + " " + f).strip()
    if buf:
        parti.append(buf)
    return parti


def prepara_motore():
    radice = os.path.join(CACHE, f"sherpa-onnx-v{SHERPA_VER}-linux-x64-shared")
    voce = os.path.join(CACHE, "vits-piper-it_IT-paola-medium")
    os.makedirs(CACHE, exist_ok=True)
    for url, cartella in ((SHERPA_URL, radice), (VOICE_URL, voce)):
        if not os.path.isdir(cartella):
            arc = os.path.join(CACHE, os.path.basename(url))
            subprocess.run(["curl", "-sSL", "--fail", "--retry", "3", "-o", arc, url], check=True)
            with tarfile.open(arc) as t:
                t.extractall(CACHE, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(CACHE)
    return radice, voce


def sintetizza(lavori, radice, voce):
    exe = os.path.join(radice, "bin", "sherpa-onnx-offline-tts")
    env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(radice, "lib"))
    base = [exe, f"--vits-model={voce}/it_IT-paola-medium.onnx", f"--vits-tokens={voce}/tokens.txt",
            f"--vits-data-dir={voce}/espeak-ng-data", "--num-threads=1", "--vits-length-scale=1.0"]

    def uno(lavoro):
        testo, out = lavoro
        for _ in range(2):
            r = subprocess.run(base + [f"--output-filename={out}", testo], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(out):
                return True
        return False

    with ThreadPoolExecutor(3) as ex:
        return list(ex.map(uno, lavori))


def pulisci_vecchi(oggi):
    limite = oggi - timedelta(days=GIORNI_AUDIO)
    for mp3 in AUDIO.glob("*.mp3"):
        try:
            g = datetime.strptime(mp3.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if g < limite:
            mp3.unlink()
            print(f"  - audio del {mp3.stem} eliminato (più vecchio di {GIORNI_AUDIO} giorni)")


def main():
    giorni = sorted(p.stem for p in DATA.glob("*.json") if re.fullmatch(r"\d{4}-\d\d-\d\d", p.stem))
    if not giorni:
        sys.exit("Nessuna edizione da leggere.")
    giorno = sys.argv[1] if len(sys.argv) > 1 else giorni[-1]
    f = DATA / f"{giorno}.json"
    dati = json.loads(f.read_text(encoding="utf-8"))
    if "senza AI" in str(dati.get("modello", "")):
        print("Edizione senza riassunto: niente audio.")
        return

    frasi = copione(dati)
    radice, voce = prepara_motore()
    tmp = tempfile.mkdtemp()
    lavori, pausa_dopo = [], []
    for i, frase in enumerate(frasi):
        pezzi = spezza(frase)
        for k, p in enumerate(pezzi):
            lavori.append((p, os.path.join(tmp, f"{i:04d}_{k:02d}.wav")))
            pausa_dopo.append(0.55 if k == len(pezzi) - 1 else 0.15)
    esiti = sintetizza(lavori, radice, voce)
    if sum(esiti) < len(lavori) * 0.95:
        sys.exit(f"Sintesi fallita per {len(lavori) - sum(esiti)} pezzi su {len(lavori)}")

    completo, durata, rate = os.path.join(tmp, "tutto.wav"), 0.0, None
    with wave.open(completo, "wb") as w:
        for (_, p), pausa in zip(lavori, pausa_dopo):
            if not os.path.exists(p):
                continue
            with wave.open(p, "rb") as r:
                if rate is None:
                    rate = r.getframerate()
                    w.setnchannels(1)
                    w.setsampwidth(r.getsampwidth())
                    w.setframerate(rate)
                n = r.getnframes()
                w.writeframes(r.readframes(n))
                w.writeframes(b"\x00\x00" * int(rate * pausa))
                durata += n / rate + pausa

    AUDIO.mkdir(parents=True, exist_ok=True)
    mp3 = AUDIO / f"{giorno}.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", completo, "-ac", "1", "-ar", "22050",
                    "-codec:a", "libmp3lame", "-b:a", "32k", str(mp3)], check=True)

    dati["audio"] = f"audio/{giorno}.mp3"
    dati["durata"] = round(durata, 1)
    f.write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ audio del {giorno}: {durata / 60:.1f} minuti, {mp3.stat().st_size // 1024} KB")
    pulisci_vecchi(date.fromisoformat(giorno))


if __name__ == "__main__":
    try:
        main()
    except BaseException as ex:
        if isinstance(ex, SystemExit) and ex.code in (0, None):
            raise
        avvisi = os.environ.get("AVVISI_FILE")
        if avvisi:
            with open(avvisi, "a", encoding="utf-8") as out:
                out.write(f"- Audio non creato: {str(ex)[:200]}\n")
        raise
