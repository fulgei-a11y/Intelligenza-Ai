#!/usr/bin/env python3
"""
AI News Digest — riassunto quotidiano delle notizie sull'intelligenza artificiale.

1. Legge i feed RSS/Atom elencati in sources.json
2. Tiene solo gli articoli delle ultime ORE_FINESTRA ore (default 36)
3. Chiede a Gemini un riassunto in italiano, citando solo articoli realmente letti
4. Genera pagine HTML statiche in docs/ (pubblicate con GitHub Pages)

Nessuna dipendenza esterna: solo la libreria standard di Python 3.9+.
Variabili d'ambiente:
  GEMINI_API_KEY   chiave gratuita da https://aistudio.google.com/apikey
  GEMINI_MODEL     modello da usare (default: gemini-2.5-flash)
  ORE_FINESTRA     ore di notizie da considerare (default: 36)
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA = DOCS / "data"
GIORNI = DOCS / "giorni"
TZ = ZoneInfo("Europe/Rome")

ORE_FINESTRA = int(os.environ.get("ORE_FINESTRA", "36"))
MODELLO = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
MODELLI_RISERVA = ["gemini-2.5-flash-lite"]
UA = "Mozilla/5.0 (compatible; AI-News-Digest/1.0; +https://github.com)"

PAROLE_AI = re.compile(
    r"\b(ai|a\.i\.|ia|intelligenz[ae] artificial[ei]|artificial intelligence|"
    r"chatgpt|openai|gpt[-\s]?\d|llm|gemini|claude|anthropic|copilot|deepmind|"
    r"machine learning|apprendimento automatico|deep learning|reti neurali|"
    r"chatbot|agent[ei] ai|ai act|mistral|llama|deepseek|nvidia|generativ[ao])\b",
    re.IGNORECASE,
)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


# ---------------------------------------------------------------- lettura feed

_ultimo_accesso = {}


def scarica(url: str, timeout: int = 25) -> bytes:
    """Scarica un feed. Aspetta tra due richieste allo stesso sito e riprova se il sito
    risponde 'troppe richieste' (429) o è temporaneamente non disponibile."""
    host = urllib.parse.urlparse(url).netloc
    for tentativo in range(3):
        attesa = 6 - (time.time() - _ultimo_accesso.get(host, 0))
        if attesa > 0:
            time.sleep(attesa)
        _ultimo_accesso[host] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and tentativo < 2:
                time.sleep(15 * (tentativo + 1))
                continue
            raise


def pulisci_testo(s: str, limite: int = 400) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[: limite - 1] + "…") if len(s) > limite else s


def leggi_data(testo: str):
    if not testo:
        return None
    testo = testo.strip()
    try:
        d = parsedate_to_datetime(testo)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(testo.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def testo_di(el, *percorsi):
    for p in percorsi:
        x = el.find(p, NS)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def analizza_feed(contenuto: bytes):
    radice = ET.fromstring(contenuto)
    articoli = []
    # RSS 2.0 / RDF
    items = radice.findall(".//item") + radice.findall(".//{http://purl.org/rss/1.0/}item")
    for it in items:
        articoli.append({
            "titolo": testo_di(it, "title", "{http://purl.org/rss/1.0/}title"),
            "link": testo_di(it, "link", "{http://purl.org/rss/1.0/}link"),
            "data": leggi_data(testo_di(it, "pubDate", "dc:date")),
            "testo": pulisci_testo(testo_di(it, "description", "content:encoded",
                                            "{http://purl.org/rss/1.0/}description")),
        })
    # Atom
    for en in radice.findall("atom:entry", NS):
        link = ""
        for l in en.findall("atom:link", NS):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        articoli.append({
            "titolo": pulisci_testo(testo_di(en, "atom:title"), 300),
            "link": link,
            "data": leggi_data(testo_di(en, "atom:published", "atom:updated")),
            "testo": pulisci_testo(testo_di(en, "atom:summary", "atom:content")),
        })
    return articoli


def raccogli(fonti, limite_tempo):
    tutti, stato = [], []
    visti = set()
    for f in fonti:
        try:
            grezzi = analizza_feed(scarica(f["url"]))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError, ValueError) as e:
            stato.append({"nome": f["nome"], "ok": False, "errore": str(e)[:120], "presi": 0})
            print(f"  ✗ {f['nome']}: {e}", file=sys.stderr)
            continue
        presi = 0
        for a in grezzi:
            if not a["titolo"] or not a["link"].startswith("http"):
                continue
            if a["data"] and a["data"] < limite_tempo:
                continue
            if f.get("filtro_ai") and not PAROLE_AI.search(a["titolo"] + " " + a["testo"]):
                continue
            chiave = re.sub(r"\W+", "", a["titolo"].lower())[:80]
            if chiave in visti:
                continue
            visti.add(chiave)
            a["fonte"] = f["nome"]
            a["categoria"] = f["categoria"]
            tutti.append(a)
            presi += 1
            if presi >= f.get("max", 10):
                break
        stato.append({"nome": f["nome"], "ok": True, "presi": presi})
        print(f"  ✓ {f['nome']}: {presi} articoli")
    for i, a in enumerate(tutti, 1):
        a["id"] = f"a{i}"
    return tutti, stato


# ---------------------------------------------------------------- Gemini

ISTRUZIONI = """Sei un giornalista esperto di intelligenza artificiale che scrive una rassegna
quotidiana in ITALIANO per un lettore curioso, non necessariamente tecnico, che vuole
restare informato e scoprire nuove possibilità concrete offerte dall'AI.

Ricevi un elenco di articoli, ciascuno con un id (a1, a2, ...). Regole tassative:
- Usa SOLO le informazioni presenti negli articoli forniti. Non inventare fatti, cifre o nomi.
- Ogni notizia deve citare in "fonti" gli id degli articoli da cui proviene.
- Raggruppa articoli che parlano dello stesso fatto in un'unica notizia.
- Scegli le notizie davvero rilevanti (massimo 12 in totale), scarta pubblicità e rumore.
- Scrivi in modo chiaro, senza gergo inutile; spiega i termini tecnici in poche parole.
- Se un fatto è riportato solo da post di community (Reddit, Hacker News), dillo esplicitamente.

Rispondi SOLO con JSON valido in questo formato:
{
  "titolo_giorno": "titolo breve che riassume la giornata",
  "in_breve": "2-3 frasi con il quadro generale della giornata",
  "sezioni": [
    {"titolo": "Le notizie principali", "notizie": [
      {"titolo": "...", "riassunto": "2-4 frasi", "perche_conta": "1 frase", "fonti": ["a1"]}
    ]},
    {"titolo": "Dai laboratori AI", "notizie": [...]},
    {"titolo": "Dall'Italia e dall'Europa", "notizie": [...]},
    {"titolo": "Ricerca", "notizie": [...]},
    {"titolo": "Dalla community", "notizie": [...]}
  ],
  "da_provare": [
    {
      "cosa": "nome dello strumento, funzione o servizio",
      "a_cosa_serve": "1-2 frasi: cosa permette di fare nella vita o nel lavoro di tutti i giorni, con un esempio concreto",
      "come_iniziare": ["passo 1", "passo 2", "passo 3"],
      "costo": "Gratis / A pagamento / Gratis con limiti / Non indicato nelle fonti",
      "difficolta": "Facile / Media / Per esperti",
      "fonti": ["a3"]
    }
  ]
}
Ometti le sezioni vuote.

LA SEZIONE "da_provare" È LA PIÙ IMPORTANTE della rassegna: il lettore vuole scoprire cosa può
fare di nuovo con l'AI. Cercala con cura in TUTTI gli articoli (lanci di prodotti, nuove funzioni
di ChatGPT/Gemini/Claude/Copilot, app, strumenti open source, tutorial, casi d'uso interessanti).
- Inserisci da 3 a 6 elementi quando le fonti lo permettono; mai inventarne per arrivare al numero.
- Solo cose che una persona può usare davvero oggi o a breve, non risultati di ricerca teorici.
- Preferisci ciò che è utilizzabile da chi non è programmatore; gli strumenti tecnici segnali come "Per esperti".
- "come_iniziare" deve contenere 2-4 passi concreti basati sulle fonti (dove andare, cosa cercare);
  se le fonti non lo dicono, scrivi un solo passo: "Leggi l'articolo per i dettagli".
- Per costo e disponibilità in Italia/UE riporta solo ciò che è scritto nelle fonti; altrimenti "Non indicato nelle fonti".
- Una novità può comparire sia tra le notizie sia in "da_provare"."""


def chiedi_a_gemini(articoli, chiave):
    elenco = "\n".join(
        f"[{a['id']}] ({a['categoria']} · {a['fonte']}) {a['titolo']} — {a['testo']}"
        for a in articoli
    )
    corpo = {
        "systemInstruction": {"parts": [{"text": ISTRUZIONI}]},
        "contents": [{"role": "user", "parts": [{"text": "ARTICOLI DI OGGI:\n" + elenco}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    dati = json.dumps(corpo).encode()
    ultimo_errore = None
    for modello in [MODELLO] + [m for m in MODELLI_RISERVA if m != MODELLO]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
        for tentativo in range(3):
            req = urllib.request.Request(url, data=dati, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": chiave})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    risposta = json.loads(r.read())
                testo = risposta["candidates"][0]["content"]["parts"][0]["text"]
                testo = re.sub(r"^```(?:json)?|```$", "", testo.strip()).strip()
                print(f"  ✓ riassunto generato con {modello}")
                return json.loads(testo), modello
            except urllib.error.HTTPError as e:
                ultimo_errore = f"{modello}: HTTP {e.code} {e.read()[:200]!r}"
                print("  ✗ " + ultimo_errore, file=sys.stderr)
                if e.code in (429, 500, 503):
                    time.sleep(20 * (tentativo + 1))
                    continue
                break
            except (KeyError, IndexError, json.JSONDecodeError, OSError) as e:
                ultimo_errore = f"{modello}: {e}"
                print("  ✗ " + ultimo_errore, file=sys.stderr)
                time.sleep(5)
    raise RuntimeError(ultimo_errore or "Gemini non disponibile")


def verifica(riassunto, per_id):
    """Scarta tutto ciò che non cita articoli realmente letti (niente link inventati)."""
    sezioni = []
    for s in riassunto.get("sezioni", []):
        notizie = []
        for n in s.get("notizie", []):
            n["fonti"] = [i for i in n.get("fonti", []) if i in per_id]
            if n["fonti"] and n.get("titolo"):
                notizie.append(n)
        if notizie:
            sezioni.append({"titolo": s.get("titolo", ""), "notizie": notizie})
    riassunto["sezioni"] = sezioni
    riassunto["da_provare"] = [
        d for d in riassunto.get("da_provare", [])
        if (d.update(fonti=[i for i in d.get("fonti", []) if i in per_id]) or d["fonti"])
    ]
    return riassunto


def senza_ai(articoli):
    """Versione di riserva: titoli raggruppati per categoria, senza riassunto."""
    ordine = ["Laboratori", "Testate tech", "Italia", "Ricerca", "Community"]
    sezioni = []
    for c in ordine:
        gruppo = [a for a in articoli if a["categoria"] == c]
        if gruppo:
            sezioni.append({"titolo": c, "notizie": [
                {"titolo": a["titolo"], "riassunto": a["testo"][:250], "fonti": [a["id"]]}
                for a in gruppo]})
    return {
        "titolo_giorno": "Le notizie AI di oggi",
        "in_breve": "Riassunto automatico non disponibile oggi: ecco i titoli raccolti dalle fonti.",
        "sezioni": sezioni,
        "da_provare": [],
    }


# ---------------------------------------------------------------- HTML

CSS = """
:root{--bg:#f7f5f0;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e0d8;--accent:#c2410c;--chip:#f1ede4}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#9c9a93;--line:#2e2d2a;--accent:#fb923c;--chip:#2a2926}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 Georgia,'Iowan Old Style',serif}
main{max-width:720px;margin:0 auto;padding:28px 16px 60px}
.top{font:600 13px/1.4 system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
h1{font-size:30px;line-height:1.2;margin:6px 0 12px}h2{font:700 14px/1.3 system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:40px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.lead{font-size:19px;color:var(--ink)}.meta{font:13px system-ui,sans-serif;color:var(--muted)}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:12px 0}
article h3{margin:0 0 6px;font-size:19px;line-height:1.3}article p{margin:6px 0}
.why{font-size:15px;color:var(--muted)}.why b{color:var(--accent);font-weight:600}
.src{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.src a{font:12px system-ui,sans-serif;background:var(--chip);color:var(--ink);text-decoration:none;padding:4px 9px;border-radius:999px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src a:hover{color:var(--accent)}
.tryzone{background:var(--chip);border-radius:16px;padding:4px 16px 12px;margin:28px -4px 0}
.tryzone h2{color:var(--accent);border-bottom:none;margin:18px 0 0;font-size:16px}
.sub{font:14px system-ui,sans-serif;color:var(--muted);margin:4px 0 8px}
.try{border-left:3px solid var(--accent)}.try ol{margin:4px 0 0;padding-left:22px}.try li{margin:3px 0}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.tag{font:600 11px system-ui,sans-serif;letter-spacing:.03em;text-transform:uppercase;color:var(--accent);border:1px solid var(--accent);border-radius:999px;padding:2px 8px}
.arch a{color:var(--ink)}.arch li{margin:4px 0}
details{margin-top:40px;font:14px system-ui,sans-serif;color:var(--muted)}
.ko{color:#dc2626}
"""


def e(s):
    return html.escape(str(s or ""))


def chips(ids, per_id):
    out = []
    for i in ids:
        a = per_id[i]
        out.append(f'<a href="{e(a["link"])}" target="_blank" rel="noopener" title="{e(a["titolo"])}">'
                   f'{e(a["fonte"])} ↗</a>')
    return '<div class="src">' + "".join(out) + "</div>"


def pagina(giorno, r, per_id, stato, modello, archivio, prefisso):
    data_it = giorno.strftime("%A %d %B %Y")
    parti = [f'<p class="top">AI News Digest · {e(data_it)}</p>',
             f"<h1>{e(r.get('titolo_giorno'))}</h1>",
             f'<p class="lead">{e(r.get("in_breve"))}</p>']
    if r.get("da_provare"):
        parti.append('<section class="tryzone"><h2>🧪 Da provare oggi</h2>'
                     '<p class="sub">Nuovi strumenti e possibilità emersi dalle notizie di oggi</p>')
        for d in r["da_provare"]:
            tag = "".join(f'<span class="tag">{e(t)}</span>'
                          for t in (d.get("difficolta"), d.get("costo")) if t)
            passi = d.get("come_iniziare") or d.get("come") or []
            if isinstance(passi, str):
                passi = [passi]
            lista = ("<ol>" + "".join(f"<li>{e(p)}</li>" for p in passi) + "</ol>") if passi else ""
            parti.append(f'<article class="try"><div class="tags">{tag}</div>'
                         f'<h3>{e(d.get("cosa"))}</h3><p>{e(d.get("a_cosa_serve"))}</p>'
                         f'{"<p class=why><b>Come iniziare</b></p>" + lista if lista else ""}'
                         f"{chips(d['fonti'], per_id)}</article>")
        parti.append("</section>")
    for s in r["sezioni"]:
        parti.append(f"<h2>{e(s['titolo'])}</h2>")
        for n in s["notizie"]:
            perche = (f'<p class="why"><b>Perché conta:</b> {e(n["perche_conta"])}</p>'
                      if n.get("perche_conta") else "")
            parti.append(f"<article><h3>{e(n['titolo'])}</h3><p>{e(n.get('riassunto'))}</p>"
                         f"{perche}{chips(n['fonti'], per_id)}</article>")
    if archivio:
        voci = "".join(f'<li><a href="{prefisso}giorni/{g}.html">{g}</a></li>' for g in archivio[:60])
        parti.append(f'<h2>Archivio</h2><ul class="arch">{voci}</ul>')
    ok = sum(1 for s in stato if s["ok"])
    righe = "".join(
        f"<li>{e(s['nome'])}: {s['presi']} articoli</li>" if s["ok"]
        else f"<li class='ko'>{e(s['nome'])}: non raggiungibile ({e(s.get('errore'))})</li>"
        for s in stato)
    parti.append(f"<details><summary>Fonti consultate: {ok}/{len(stato)} raggiungibili · "
                 f"{len(per_id)} articoli · {e(modello)}</summary><ul>{righe}</ul></details>")
    return (f'<!doctype html><html lang="it"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>AI News Digest · {giorno:%d/%m/%Y}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(parti)}</main></body></html>")


# ---------------------------------------------------------------- main

def main():
    import locale
    for loc in ("it_IT.UTF-8", "it_IT.utf8", "it_IT"):
        try:
            locale.setlocale(locale.LC_TIME, loc)
            break
        except locale.Error:
            pass

    fonti = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["fonti"]
    limite = datetime.now(timezone.utc) - timedelta(hours=ORE_FINESTRA)
    print(f"Leggo {len(fonti)} fonti (ultime {ORE_FINESTRA} ore)…")
    articoli, stato = raccogli(fonti, limite)
    if not articoli:
        print("Nessun articolo trovato: non aggiorno il sito.", file=sys.stderr)
        sys.exit(1)
    per_id = {a["id"]: a for a in articoli}

    chiave = os.environ.get("GEMINI_API_KEY", "").strip()
    modello = "senza AI"
    if chiave:
        try:
            riassunto, modello = chiedi_a_gemini(articoli, chiave)
            riassunto = verifica(riassunto, per_id)
            if not riassunto["sezioni"]:
                raise RuntimeError("riassunto vuoto")
        except Exception as ex:  # qualunque problema: si pubblica comunque la versione semplice
            print(f"Gemini non disponibile ({ex}), uso la versione senza AI.", file=sys.stderr)
            riassunto, modello = senza_ai(articoli), "senza AI (Gemini non disponibile)"
    else:
        print("GEMINI_API_KEY non impostata: versione senza AI.")
        riassunto = senza_ai(articoli)

    oggi = datetime.now(TZ)
    giorno = oggi.strftime("%Y-%m-%d")
    for d in (DATA, GIORNI):
        d.mkdir(parents=True, exist_ok=True)
    usati = {i for s in riassunto["sezioni"] for n in s["notizie"] for i in n["fonti"]}
    usati |= {i for d in riassunto.get("da_provare", []) for i in d["fonti"]}
    salvati = {i: {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in per_id[i].items()}
               for i in usati}
    (DATA / f"{giorno}.json").write_text(json.dumps(
        {"giorno": giorno, "modello": modello, "riassunto": riassunto, "articoli": salvati, "fonti": stato},
        ensure_ascii=False, indent=1), encoding="utf-8")

    archivio = sorted((p.stem for p in DATA.glob("*.json")), reverse=True)
    (GIORNI / f"{giorno}.html").write_text(
        pagina(oggi, riassunto, per_id, stato, modello, archivio, "../"), encoding="utf-8")
    (DOCS / "index.html").write_text(
        pagina(oggi, riassunto, per_id, stato, modello, archivio, ""), encoding="utf-8")
    print(f"Fatto: docs/index.html e docs/giorni/{giorno}.html")


if __name__ == "__main__":
    main()
