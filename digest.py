#!/usr/bin/env python3
"""
AI News Digest — guida quotidiana ai nuovi strumenti di intelligenza artificiale, per il lavoro e per
tutti i giorni: strumenti nuovi, novità negli strumenti che già si usano, al massimo 3 notizie "In breve".

1. Legge i feed RSS/Atom elencati in sources.json (ultime ORE_FINESTRA ore)
2. Toglie gli articoli già usati negli ultimi 14 giorni (niente doppioni, niente strumenti ripetuti)
3. Scarica il testo completo degli articoli che hanno solo il titolo (anche da Google News)
4. Chiede a Gemini un riassunto in italiano, citando solo articoli realmente letti,
   poi applica regole fisse: etichette di affidabilità, "Solo titoli", niente accuse senza fonti solide
5. La domenica crea anche "La settimana dell'AI"
6. Genera le pagine HTML statiche in docs/ (GitHub Pages), con archivio, ricerca e glossario

Uso:  python digest.py              edizione di oggi
      python digest.py --pagine     ricostruisce solo le pagine dai dati salvati
      python digest.py --settimana  crea ora "La settimana dell'AI"

Librerie facoltative (se mancano si usa una riserva con la sola libreria standard):
  trafilatura (estrae il testo degli articoli), googlenewsdecoder (link originali di Google News)
Variabili d'ambiente:
  GEMINI_API_KEY   chiave gratuita da https://aistudio.google.com/apikey
  GEMINI_MODEL     modello preferito (default: gemini-3.5-flash, con riserve automatiche)
  ORE_FINESTRA     ore di notizie da considerare (default: 36)
  AVVISI_FILE      file dove annotare i problemi da segnalare
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

SETTIMANE = DOCS / "settimana"
AUDIO = DOCS / "audio"

ORE_FINESTRA = int(os.environ.get("ORE_FINESTRA", "36"))
# Il primo modello disponibile vince; se uno viene ritirato (errore 404) si passa al successivo.
MODELLI = [m for m in dict.fromkeys([
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite",
]) if m]
UA = "Mozilla/5.0 (compatible; AI-News-Digest/1.0; +https://github.com)"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_DA_LEGGERE = int(os.environ.get("MAX_DA_LEGGERE", "70"))   # articoli di cui scaricare il testo
MAX_TESTO = 1800                                                # caratteri di testo per articolo
TESTO_MINIMO = 350                                              # sotto questa soglia è "solo titolo"
AVVISI = []                                                     # problemi da segnalare al proprietario

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
            with urllib.request.urlopen(req, timeout=timeout + 15 * tentativo) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and tentativo < 2:
                time.sleep(15 * (tentativo + 1))
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            # Siti lenti (es. hnrss.org): un secondo tentativo con più tempo a disposizione.
            if tentativo < 1 and ("timed out" in str(e) or isinstance(e, TimeoutError)):
                time.sleep(5)
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


EDITORI_UFFICIALI = {
    "openai", "openai.com", "blog.google", "google", "google blog", "google deepmind", "deepmind.google",
    "anthropic", "anthropic.com", "www.anthropic.com", "meta", "www.meta.com", "about.fb.com",
    "ai.meta.com", "microsoft", "blogs.microsoft.com", "nvidia", "nvidia blog", "hugging face",
    "mistral ai", "commissione europea", "european commission", "governo.it", "agid",
}


TESTATE_AFFIDABILI = {
    # internazionali
    "reuters", "associated press", "ap news", "bloomberg", "financial times", "the wall street journal",
    "wall street journal", "the new york times", "new york times", "the washington post", "the guardian",
    "bbc", "bbc news", "cnn", "cnbc", "cbs news", "nbc news", "abc news", "npr", "axios", "politico",
    "the economist", "the atlantic", "wired", "the verge", "techcrunch", "ars technica", "engadget",
    "mit technology review", "ieee spectrum", "zdnet", "cnet", "pcmag", "tom's guide", "tom's hardware",
    "9to5google", "9to5mac", "android authority", "android police", "macrumors", "the register",
    "venturebeat", "fortune", "forbes", "business insider", "fast company", "nature", "science",
    "new scientist", "scientific american", "platformer", "semafor", "the information", "le monde",
    "euronews", "techrepublic", "infoworld", "computerworld", "mashable", "gizmodo", "time", "vox",
    "the hill", "search engine land", "the decoder", "404 media", "rest of world", "nikkei asia",
    # italiane
    "ansa", "ansa.it", "il sole 24 ore", "corriere della sera", "corriere.it", "la repubblica",
    "repubblica", "la stampa", "il post", "rainews", "rai news", "sky tg24", "tgcom24", "agi",
    "adnkronos", "il fatto quotidiano", "il messaggero", "wired italia", "wired.it", "agenda digitale",
    "agendadigitale.eu", "punto informatico", "hdblog", "hdblog.it", "hardware upgrade", "dday.it",
    "corriere comunicazioni", "corcom", "ipsoa", "milano finanza", "startupitalia", "il foglio",
    "avvenire", "open", "fanpage.it", "geopop", "tom's hardware italia", "money.it",
}


def norm_editore(nome):
    n = nome.lower().strip()
    return n[4:] if n.startswith("www.") else n


def separa_editore(titolo):
    """I titoli di Google News finiscono con ' - Editore': li separiamo."""
    m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]{2,60})$", titolo)
    return (m.group(1), m.group(2).strip()) if m else (titolo, "")


def raccogli(fonti, limite_tempo, editori_esclusi=()):
    tutti, stato = [], []
    visti = set()
    esclusi = {x.lower() for x in editori_esclusi}
    for f in fonti:
        escludi = re.compile(f["escludi"], re.IGNORECASE) if f.get("escludi") else None
        try:
            grezzi = analizza_feed(scarica(f["url"]))
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError, ValueError) as e:
            stato.append({"nome": f["nome"], "ok": False, "errore": str(e)[:120], "presi": 0})
            print(f"  ✗ {f['nome']}: {e}", file=sys.stderr)
            continue
        presi = 0
        limite_fonte = (datetime.now(timezone.utc) - timedelta(hours=f["ore"])) if f.get("ore") else limite_tempo
        for a in grezzi:
            if not a["titolo"] or not a["link"].startswith("http"):
                continue
            if a["data"] and a["data"] < limite_fonte:
                continue
            if f.get("tipo") == "vetrina":
                # Product Hunt: "slogan Discussion | Link"; Show HN: "Article URL: … Points: …"
                a["testo"] = re.sub(r"\s*Discussion\s*\|\s*Link\s*$", "", a["testo"])
                a["testo"] = re.sub(r"(Article URL|Comments URL|Points|# Comments):\s*\S*", " ", a["testo"]).strip()
                a["titolo"] = re.sub(r"^Show HN:\s*", "", a["titolo"])
            if f.get("filtro_ai") and not PAROLE_AI.search(a["titolo"] + " " + a["testo"]):
                continue
            if escludi and escludi.search(a["titolo"]):
                continue
            tipo = f.get("tipo", "testata")
            fonte = f["nome"]
            if "news.google.com" in f["url"]:
                a["titolo"], editore = separa_editore(a["titolo"])
                if editore:
                    if editore.lower() in esclusi:
                        continue
                    fonte = editore
                    n = norm_editore(editore)
                    if n in EDITORI_UFFICIALI or editore.lower() in EDITORI_UFFICIALI:
                        tipo = "ufficiale"
                    elif n in TESTATE_AFFIDABILI:
                        tipo = "testata"
                    else:
                        tipo = "minore"
                a["testo"] = ""  # Google News fornisce solo il titolo, mai il testo
            chiave = re.sub(r"\W+", "", a["titolo"].lower())[:80]
            if chiave in visti:
                continue
            visti.add(chiave)
            a["fonte"] = fonte
            a["tipo"] = tipo
            a["solo_titolo"] = not a["testo"]
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


# ---------------------------------------------------------------- testo completo degli articoli
#
# I feed spesso danno solo il titolo (Google News sempre). Senza testo il modello è costretto a
# indovinare i dettagli: qui scarichiamo l'articolo vero e ne estraiamo il testo.

def risolvi_google(url):
    """Trasforma un link news.google.com nel link dell'articolo originale ('' se non ci riesce)."""
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        try:
            from googlenewsdecoder import new_decoderv1 as gnewsdecoder
        except ImportError:
            return ""
    try:
        r = gnewsdecoder(url, interval=1)
        if isinstance(r, dict) and r.get("status") and str(r.get("decoded_url", "")).startswith("http"):
            return r["decoded_url"]
    except Exception as ex:  # la libreria dipende da Google: qualunque errore = rinuncia
        print(f"    (link Google non decodificato: {str(ex)[:80]})", file=sys.stderr)
    return ""


def scarica_pagina(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA_BROWSER, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tipo = r.headers.get("Content-Type", "")
        if "html" not in tipo and "xml" not in tipo:
            return ""
        grezzo = r.read(3_000_000)
        cs = r.headers.get_content_charset() or "utf-8"
    return grezzo.decode(cs, errors="replace")


def estrai_testo(pagina_html, url=""):
    testo = ""
    try:
        import trafilatura
        testo = trafilatura.extract(pagina_html, url=url, include_comments=False,
                                    include_tables=False, favor_precision=True) or ""
    except ImportError:
        pass
    except Exception:
        testo = ""
    if len(testo) < TESTO_MINIMO:
        # Riserva senza librerie: descrizione della pagina + paragrafi lunghi.
        corpo = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form)\b.*?</\1>", " ", pagina_html)
        meta = re.search(r'(?is)<meta[^>]+(?:property|name)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']+)',
                         corpo)
        paragrafi = [pulisci_testo(p, 2000) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", corpo)]
        paragrafi = [p for p in paragrafi if len(p) > 80]
        riserva = " ".join(([html.unescape(meta.group(1))] if meta else []) + paragrafi)
        if len(riserva) > len(testo):
            testo = riserva
    testo = re.sub(r"\s+", " ", testo).strip()
    return (testo[: MAX_TESTO - 1] + "…") if len(testo) > MAX_TESTO else testo


def arricchisci(articoli):
    """Aggiunge il testo completo agli articoli che ne hanno poco. Restituisce quanti ne ha arricchiti."""
    from concurrent.futures import ThreadPoolExecutor
    ordine = {"vetrina": 0, "ufficiale": 1, "testata": 2, "minore": 3}
    candidati = [a for a in articoli
                 if a.get("tipo") in ordine and a.get("categoria") != "Ricerca"
                 and len(a.get("testo", "")) < TESTO_MINIMO]
    candidati.sort(key=lambda a: ordine[a["tipo"]])
    candidati = candidati[:MAX_DA_LEGGERE]
    if not candidati:
        return 0
    print(f"Leggo il testo completo di {len(candidati)} articoli…")

    # 1) I link di Google News vanno decodificati uno alla volta, con calma.
    for a in candidati:
        if "news.google.com" in a["link"]:
            vero = risolvi_google(a["link"])
            if vero:
                a["link"] = vero          # anche il lettore finirà sull'articolo vero

    # 2) Scarichiamo le pagine in parallelo.
    def leggi(a):
        if "news.google.com" in a["link"]:
            return a, ""
        try:
            return a, estrai_testo(scarica_pagina(a["link"]), a["link"])
        except Exception:
            return a, ""

    arricchiti = 0
    with ThreadPoolExecutor(8) as ex:
        for a, testo in ex.map(leggi, candidati):
            if len(testo) >= TESTO_MINIMO and len(testo) > len(a.get("testo", "")):
                a["testo"] = testo
                a["solo_titolo"] = False
                a["testo_completo"] = True
                arricchiti += 1
    print(f"  ✓ testo completo per {arricchiti} articoli su {len(candidati)}")
    return arricchiti


# ---------------------------------------------------------------- Gemini

ISTRUZIONI = """Sei il redattore di "AI News Digest", una guida quotidiana in ITALIANO agli STRUMENTI di
intelligenza artificiale. Il lettore è una persona italiana NON programmatrice che vuole una cosa sola:
scoprire AI nuove e cose utili da usare nel lavoro (ufficio, professione, piccola attività, studio) e nella
vita di tutti i giorni (casa, famiglia, salute, viaggi, foto, burocrazia, hobby).
NON è un notiziario: politica, cause legali, licenziamenti, finanziamenti, borsa, polemiche e ricerca
scientifica interessano solo se cambiano subito cosa il lettore può usare.

Ricevi articoli nel formato: [id] {tipo · editore · sezione} titolo — estratto
Tipi di fonte:
- ufficiale: comunicato o blog dell'azienda interessata (affidabile sui fatti, ma è autopromozione)
- testata: giornale o rivista affidabile
- vetrina: prodotto appena presentato dal suo stesso creatore (Product Hunt, Show HN). Prova che lo
  strumento esiste e si può provare, non che funzioni bene: descrivilo per quello che dichiara di fare.
- minore: sito non verificato: usalo solo se confermato da altre fonti o se è una vetrina di strumenti
- community: post di Reddit, NON verificato
Molti articoli arrivano SOLO CON IL TITOLO: in quel caso sai CHE COSA è successo ma non i dettagli.
Non dedurre dettagli che il titolo non dice. Conta solo ciò che è scritto negli articoli.

REGOLE SUI FATTI (tassative)
1. Usa SOLO informazioni presenti negli articoli. Non inventare nomi, prezzi, menu, siti, date, paesi.
2. Prezzo, lingua italiana, disponibilità in Italia, piattaforma (iPhone, Android, Mac, Windows, web):
   scrivili SOLO se un articolo li dice. Altrimenti usa "Non indicato".
3. "come_iniziare": 2-4 passi concreti SOLO se un articolo li descrive davvero; altrimenti lista vuota [].
   MAI frasi vuote come "apri l'articolo".
4. MAI riportare accuse (reati, frodi, scandali) contro persone o aziende.
5. Se ricevi l'elenco "GIÀ SEGNALATI DI RECENTE", non riproporre quegli strumenti e quelle novità, a meno
   che oggi ci sia uno sviluppo nuovo e concreto (es. arriva in Italia, diventa gratis): in quel caso
   racconta SOLO la novità.
6. Scrivi chiaro, frasi brevi, niente gergo; spiega ogni termine tecnico in poche parole.
   Scrivi sempre "AI" (mai "IA"), salvo nei nomi propri.

COSA SCEGLIERE
A) "strumenti_nuovi" — LA SEZIONE PIÙ IMPORTANTE (da 0 a 6, meglio 3 ottimi che 6 mediocri)
   App, siti, estensioni, servizi AI NUOVI o appena arrivati, usabili da un privato o da un piccolo
   professionista. Criteri, tutti obbligatori:
   - si usa senza programmare (niente API, SDK, librerie, framework, strumenti per sviluppatori,
     infrastruttura, modelli da installare su un proprio server);
   - non è riservato alle grandi aziende;
   - ha un uso concreto: in "a_cosa_serve" scrivi un esempio realistico nella vita di un italiano;
   - NON sono strumenti: casi studio di clienti, partnership, risultati di ricerca, finanziamenti.
   Preferisci ciò che è gratuito o ha una prova gratuita, funziona in italiano o è disponibile in Italia.
   Se oggi non c'è nulla di valido, lascia la lista vuota.
B) "novita_strumenti" — "Novità negli strumenti che già usi" (da 0 a 5)
   Nuove funzioni concrete di ChatGPT, Gemini, Claude, Copilot, Meta AI, Perplexity, app Google,
   Microsoft, Apple, Samsung e simili. Spiega cosa cambia per chi le usa e, se le fonti lo dicono,
   come provarla e se è già disponibile in Italia.
   ESCLUDI: modelli per soli sviluppatori, prodotti per aziende (Google Cloud, Azure, AWS, "Enterprise",
   piani aziendali, agenti per i sistemi aziendali), novità riservate ai clienti business.
   Vanno bene i piani per privati (anche a pagamento) e gli strumenti per liberi professionisti.
C) "in_breve" — DA 0 A 3, spesso 0. Solo notizie che cambiano qualcosa per chi USA l'AI in Italia:
   - una legge o regola che cambia cosa si può usare (es. AI Act, Garante privacy);
   - un rischio concreto per gli utenti (truffe con l'AI, falle di sicurezza in app diffuse, deepfake);
   - un servizio diffuso che chiude, cambia prezzo o arriva/sparisce in Italia.
   NON vanno in "in_breve": guerre e geopolitica, data center ed energia, accordi tra aziende,
   finanziamenti, borsa, cause legali, licenziamenti, studi scientifici, politica estera.
   Una frase ciascuna. Se non c'è nulla che rispetti questi criteri, lista vuota. Mai notizie da community.

Rispondi SOLO con JSON valido:
{
  "titolo_giorno": "titolo breve e concreto, centrato sullo strumento o la novità più utile del giorno",
  "presentazione": "2-3 frasi: lo strumento più interessante di oggi e per chi è utile. Niente frasi sulla rassegna stessa",
  "strumenti_nuovi": [
    {"cosa": "nome dello strumento", "a_cosa_serve": "1-2 frasi con un esempio concreto",
     "per_chi": "Lavoro / Tutti i giorni / Lavoro e tutti i giorni / Studio",
     "ambito": "2-3 parole, es. Scrittura, Foto e video, Organizzazione, Ricerca, Casa, Salute, Viaggi",
     "come_iniziare": ["passo 1", "passo 2"],
     "costo": "Gratis / Gratis con limiti / A pagamento / Prova gratuita / Non indicato",
     "piattaforma": "es. Web, iPhone, Android, Mac, Windows, estensione Chrome / Non indicato",
     "italiano": "Sì / No / Non indicato",
     "difficolta": "Facile / Media", "fonti": ["a3"]}
  ],
  "novita_strumenti": [
    {"strumento": "ChatGPT", "titolo": "...", "cosa_cambia": "2-3 frasi pratiche",
     "come_provarla": "1 frase, solo se le fonti lo dicono, altrimenti stringa vuota",
     "disponibilita": "Disponibile ora / In arrivo / Non in Italia / Non indicato", "fonti": ["a5"]}
  ],
  "in_breve": [
    {"titolo": "...", "frase": "1 frase", "fonti": ["a9"]}
  ]
}
Ogni elemento deve avere almeno un id valido in "fonti"."""


def chiedi_a_gemini(articoli, chiave, gia_pubblicate=()):
    elenco = "\n".join(
        f"[{a['id']}] {{{a.get('tipo', 'testata')} · {a['fonte']} · {a['categoria']}}} {a['titolo']}"
        + (f" — {a['testo']}" if not a.get("solo_titolo") and a["testo"]
           else "  [SOLO TITOLO, nessun testo disponibile]")
        for a in articoli
    )
    testo = "ARTICOLI DI OGGI:\n" + elenco
    if gia_pubblicate:
        testo = ("GIÀ SEGNALATI DI RECENTE (non riproporli senza sviluppi nuovi):\n"
                 + "\n".join(f"- {t}" for t in gia_pubblicate) + "\n\n" + testo)
    risposta, modello = chiama_gemini(ISTRUZIONI, testo, chiave)
    return normalizza_risposta(risposta), modello


SEZ_NOVITA = "Novità negli strumenti che già usi"
SEZ_BREVE = "In breve"


def normalizza_risposta(r):
    """Porta la risposta di Gemini al formato salvato (lo stesso dell'archivio: da_provare + sezioni)."""
    if "sezioni" in r and "strumenti_nuovi" not in r:
        return r                                   # formato vecchio: lo lasciamo com'è
    novita = [{"titolo": n.get("titolo", ""), "strumento": n.get("strumento", ""),
               "riassunto": n.get("cosa_cambia", ""), "come_provarla": n.get("come_provarla", ""),
               "disponibilita": n.get("disponibilita", ""), "fonti": n.get("fonti", [])}
              for n in r.get("novita_strumenti", []) or []]
    brevi = [{"titolo": n.get("titolo", ""), "riassunto": n.get("frase", ""), "fonti": n.get("fonti", [])}
             for n in (r.get("in_breve") if isinstance(r.get("in_breve"), list) else []) or []]
    sezioni = []
    if novita:
        sezioni.append({"titolo": SEZ_NOVITA, "notizie": novita})
    if brevi:
        sezioni.append({"titolo": SEZ_BREVE, "breve": True, "notizie": brevi})
    return {"titolo_giorno": r.get("titolo_giorno", ""),
            "in_breve": r.get("presentazione") or (r.get("in_breve") if isinstance(r.get("in_breve"), str) else ""),
            "da_provare": r.get("strumenti_nuovi", []) or [], "sezioni": sezioni, "formato": 2}


def chiama_gemini(istruzioni, testo, chiave):
    corpo = {
        "systemInstruction": {"parts": [{"text": istruzioni}]},
        "contents": [{"role": "user", "parts": [{"text": testo}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    dati = json.dumps(corpo).encode()
    ultimo_errore = None
    for modello in MODELLI:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
        for tentativo in range(3):
            req = urllib.request.Request(url, data=dati, method="POST", headers={
                "Content-Type": "application/json", "x-goog-api-key": chiave})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    risposta = json.loads(r.read())
                parti = risposta["candidates"][0]["content"]["parts"]
                testo = "".join(p.get("text", "") for p in parti if not p.get("thought"))
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


PRIORITA_TIPO = {"ufficiale": 0, "vetrina": 1, "testata": 1, "minore": 2, "ricerca": 3, "community": 4}
MAX_LINK = 4
PASSO_ONESTO = "Apri l'articolo per sapere come attivarlo"   # usato nelle edizioni vecchie
LIMITI_SEZIONE = {SEZ_NOVITA.lower(): 5, SEZ_BREVE.lower(): 3, "ricerca": 2, "dalla community": 3}
NON_INDICATO = ("non indicat", "da verificare", "non specificat", "sconosciut")


def pulisci_fonti(ids, per_id):
    """Solo id reali, senza doppioni, prima le fonti più autorevoli, al massimo MAX_LINK."""
    ids = list(dict.fromkeys(i for i in ids if i in per_id))
    ids.sort(key=lambda i: PRIORITA_TIPO.get(per_id[i].get("tipo"), 2))
    return ids


def affidabilita(ids, per_id, proposta=""):
    """Regole fisse, che il modello non può aggirare."""
    tipi = {per_id[i].get("tipo") for i in ids}
    if tipi <= {"community"}:
        return "Da verificare"
    if tipi <= {"ricerca"}:
        return "Studio non revisionato"
    if "ufficiale" in tipi:
        return "Fonte ufficiale"
    solide = {per_id[i]["fonte"] for i in ids if per_id[i].get("tipo") == "testata"}
    if not solide:
        if "vetrina" in tipi:
            return "Appena lanciato"      # presentato dal creatore: esiste, ma nessuno l'ha ancora recensito
        return "Da verificare"            # solo siti minori o community
    if len(solide) >= 2:
        return "Confermata"
    return "Una fonte" if proposta != "Da verificare" else "Da verificare"


def link_prova(ids, per_id):
    """Il link da aprire per provare lo strumento: la vetrina o il sito ufficiale, mai un articolo qualsiasi."""
    for i in ids:
        if per_id[i].get("tipo") in ("vetrina", "ufficiale") and "news.google.com" not in per_id[i]["link"]:
            return per_id[i]["link"]
    return ""


def utile(valore):
    """Vero se il campo dice qualcosa (non 'Non indicato' e simili)."""
    v = str(valore or "").strip().lower()
    return bool(v) and not any(x in v for x in NON_INDICATO)


ACCUSE = re.compile(
    r"rubat|ruba |furto|stolen|steal|truff|frod|fraud|illegal|illecit|scandal|accus|alleg|"
    r"leak|trapelat|hack|violat|breach|lawsuit|denunc|arrest|indagat|corrott|corrupt|scam|mentit|lied",
    re.IGNORECASE)


def verifica(riassunto, per_id):
    """Scarta tutto ciò che non cita articoli realmente letti e applica le regole di affidabilità."""
    sezioni = []
    for s in riassunto.get("sezioni", []):
        titolo_s = s.get("titolo", "").strip()
        community = "community" in titolo_s.lower()
        breve = s.get("breve") or titolo_s.lower() == SEZ_BREVE.lower()
        notizie = []
        for n in s.get("notizie", []):
            ids = pulisci_fonti(n.get("fonti", []), per_id)
            if not (ids and n.get("titolo")):
                continue
            tipi = {per_id[i].get("tipo") for i in ids}
            deboli = tipi <= {"community", "minore", "vetrina"}
            if deboli and ACCUSE.search(f"{n.get('titolo', '')} {n.get('riassunto', '')}"):
                print(f"  ⊘ scartata (accusa senza fonti solide): {n.get('titolo')}")
                continue
            if tipi <= {"community"} and not community:
                continue                   # le voci della community stanno solo nella loro sezione
            if breve and deboli:
                continue                   # "In breve" solo con fonti solide
            n["affidabilita"] = affidabilita(ids, per_id, n.get("affidabilita", ""))
            n["solo_titoli"] = all(per_id[i].get("solo_titolo") for i in ids)
            if n["solo_titoli"]:
                n["come_provarla"] = ""    # senza testo non sappiamo come si attiva
            n["fonti"] = ids[:MAX_LINK]
            notizie.append(n)
        notizie = notizie[: LIMITI_SEZIONE.get(titolo_s.lower(), 99)]
        if notizie:
            sezioni.append({**s, "titolo": titolo_s, "notizie": notizie})
    riassunto["sezioni"] = sezioni

    prove = []
    for d in riassunto.get("da_provare", []):
        ids = pulisci_fonti(d.get("fonti", []), per_id)
        if not (ids and d.get("cosa")):
            continue
        if all(per_id[i].get("tipo") == "ricerca" for i in ids):
            continue                       # la ricerca non è uno strumento da provare
        if ACCUSE.search(f"{d.get('cosa', '')} {d.get('a_cosa_serve', '')}") and \
                {per_id[i].get("tipo") for i in ids} <= {"community", "minore", "vetrina"}:
            continue
        d["affidabilita"] = affidabilita(ids, per_id)
        passi = d.get("come_iniziare") or []
        if isinstance(passi, str):
            passi = [passi]
        passi = [p for p in passi if p and "apri l'articolo" not in p.lower()]
        if all(per_id[i].get("solo_titolo") for i in ids):
            passi = []                     # senza testo non sappiamo come si attiva: niente passi dedotti
            d["solo_titoli"] = True
            # Dal solo titolo non si ricavano prezzo, lingua e piattaforma, salvo che il titolo li dica.
            titoli = " ".join(per_id[i]["titolo"] for i in ids).lower()
            if not re.search(r"\bfree\b|gratis|gratuit", titoli):
                d["costo"] = "Non indicato"
            if "ital" not in titoli:
                d["italiano"] = "Non indicato"
            if not re.search(r"iphone|ios|android|mac|windows|chrome|web|app\b", titoli):
                d["piattaforma"] = "Non indicato"
        d["come_iniziare"] = passi[:4]
        d["prova"] = link_prova(ids, per_id)
        d["fonti"] = ids[:MAX_LINK]
        prove.append(d)
    riassunto["da_provare"] = prove[:6]
    return riassunto


def senza_ai(articoli):
    """Versione di riserva: titoli raggruppati per categoria, senza riassunto."""
    ordine = ["Strumenti", "Laboratori", "Testate tech", "Italia", "Community"]
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
.tags{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px}.tags .rel{margin-bottom:0}
.tag{font:600 11px system-ui,sans-serif;letter-spacing:.03em;text-transform:uppercase;color:var(--accent);border:1px solid var(--accent);border-radius:999px;padding:2px 8px}
.rel{display:inline-block;font:600 11px system-ui,sans-serif;letter-spacing:.03em;text-transform:uppercase;color:var(--muted);background:var(--chip);border-radius:999px;padding:2px 8px;margin-bottom:6px}
.rel.ok{color:#15803d;background:#dcfce7}.rel.warn{color:#b45309;background:#fef3c7}
@media (prefers-color-scheme:dark){.rel.ok{color:#86efac;background:#14532d}.rel.warn{color:#fcd34d;background:#451a03}}
.legenda{font:13px system-ui,sans-serif;color:var(--muted)}
.arch a{color:var(--ink)}.arch li{margin:4px 0}
details{margin-top:40px;font:14px system-ui,sans-serif;color:var(--muted)}
.ko{color:#dc2626}
.share{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 4px}
.share button{font:600 14px system-ui,sans-serif;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:9px 14px;cursor:pointer}
.share button:active{transform:scale(.97)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:var(--bg);font:14px system-ui,sans-serif;padding:10px 16px;border-radius:999px}
@media print{
  :root{--bg:#fff;--card:#fff;--ink:#111;--muted:#555;--line:#ccc;--accent:#b4410c;--chip:#f3f3f3}
  body{font-size:12pt}main{max-width:none;padding:0}
  .share,.arch,details,.toast{display:none!important}
  h2:has(+ .arch){display:none}
  article,.tryzone{break-inside:avoid;box-shadow:none}
  .src a{border:1px solid #ccc}
  .src a::after{content:" (" attr(href) ")";font-size:8pt;color:#666;white-space:normal;word-break:break-all}
  .src a{white-space:normal;overflow:visible}
  @page{margin:14mm}
}
"""


CSS += """
.when{font:13px system-ui,sans-serif;color:var(--muted);margin:-4px 0 10px}
.rel.soft{color:var(--muted);background:transparent;border:1px dashed var(--line)}
.listen{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 14px;margin:14px 0 4px}
.listen span{font:600 14px system-ui,sans-serif}.listen audio{width:100%;height:40px}
.week{display:block;text-decoration:none;color:var(--ink);background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:12px;padding:12px 16px;margin:18px 0 0}
.week small{display:block;font:600 12px system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.week b{font-size:17px}
.glossario dl{margin:0}.glossario dt{font-weight:700;margin-top:10px}.glossario dd{margin:2px 0 0;color:var(--muted);font-size:15px}
.cerca{width:100%;font:16px system-ui,sans-serif;padding:11px 14px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--ink);margin:4px 0 10px}
.risultati{list-style:none;padding:0;margin:0 0 14px}.risultati li{padding:8px 0;border-bottom:1px solid var(--line)}
.risultati a{color:var(--ink);text-decoration:none}.risultati small{display:block;font:12px system-ui,sans-serif;color:var(--muted)}
.arch{padding-left:0;list-style:none}.arch small{font:12px system-ui,sans-serif;color:var(--muted);margin-right:6px}
.etichette dt{display:inline-block;margin-top:8px}.etichette dd{margin:2px 0 0}
article:target,li:target{outline:2px solid var(--accent)}
.prova{display:inline-block;margin:8px 8px 0 0;font:700 14px system-ui,sans-serif;color:#fff;background:var(--accent);text-decoration:none;padding:8px 16px;border-radius:999px}
.prova:active{transform:scale(.97)}
@media (prefers-color-scheme:dark){.prova{color:#1d1d1f}}
.brevi{list-style:none;padding:0;margin:0}.brevi li{padding:10px 0;border-bottom:1px solid var(--line);font-size:16px}
.brevi .src{display:inline-flex;margin:4px 0 0}
@media print{.listen,.week,.cerca,.risultati,.glossario,.prova{display:none!important}}
"""

GLOSSARIO = [
    (r"agent[ei]( ai| di intelligenza artificiale)?|\bai agents?\b", "Agente AI",
     "Un'intelligenza artificiale che non si limita a rispondere, ma compie azioni da sola (apre siti, "
     "usa programmi, scrive email) per portare a termine un compito."),
    (r"\bmodell[oi]( linguistic[oi]| di intelligenza artificiale| ai)?\b", "Modello",
     "Il \"cervello\" di un'AI come ChatGPT, Gemini o Claude: un programma addestrato su enormi quantità "
     "di testi, immagini o suoni."),
    (r"\bllm\b", "LLM", "Large Language Model, cioè modello linguistico di grandi dimensioni: il tipo di AI "
     "che sta dietro i chatbot."),
    (r"di frontiera|frontier", "Modello di frontiera", "I modelli più avanzati del momento, sviluppati dai "
     "grandi laboratori."),
    (r"open[ -]?source|pesi aperti|open[ -]?weights?", "Open source / pesi aperti",
     "Un modello che chiunque può scaricare e usare liberamente, anche sul proprio computer."),
    (r"\bai generativa|generative ai|intelligenza artificiale generativa", "AI generativa",
     "AI che crea contenuti nuovi: testi, immagini, musica, video."),
    (r"\bai act\b", "AI Act", "Il regolamento europeo sull'intelligenza artificiale: classifica gli usi "
     "dell'AI in base al rischio e vieta quelli più pericolosi."),
    (r"\bprompt\b", "Prompt", "La richiesta o l'istruzione scritta che si dà all'AI."),
    (r"allucinazion", "Allucinazione", "Quando un'AI afferma con sicurezza cose false o inventate."),
    (r"addestrament|addestrat|\btraining\b", "Addestramento", "La fase in cui un modello impara, analizzando "
     "grandi quantità di dati."),
    (r"benchmark", "Benchmark", "Un test standard usato per confrontare le capacità di AI diverse."),
    (r"deepfake", "Deepfake", "Video, foto o audio falsi ma realistici, creati con l'AI per far sembrare che "
     "una persona abbia detto o fatto qualcosa."),
    (r"multimodal", "Multimodale", "Un'AI che capisce e produce più tipi di contenuto insieme: testo, immagini, "
     "audio, video."),
    (r"\btoken\b", "Token", "I pezzetti di testo con cui un'AI legge e scrive; spesso i prezzi per gli "
     "sviluppatori si contano in token."),
    (r"\bapi\b", "API", "Il \"canale\" con cui altri programmi usano un'AI; riguarda soprattutto gli sviluppatori."),
    (r"\bgpu\b|\bchip\b", "GPU / chip", "Processori specializzati, soprattutto di Nvidia, indispensabili per "
     "addestrare e far funzionare le AI."),
    (r"data ?center", "Data center", "Enormi edifici pieni di computer dove le AI vengono addestrate e "
     "fatte funzionare; consumano molta energia."),
    (r"\bagi\b|superintelligenz", "AGI", "Un'AI capace di svolgere qualunque compito intellettuale umano: per ora "
     "un obiettivo dichiarato dei laboratori, non una realtà."),
    (r"filigran|watermark|synthid", "Filigrana digitale", "Un segno invisibile inserito nei contenuti creati "
     "dall'AI, che permette di riconoscerli."),
    (r"\barxiv\b|preprint|non revisionat", "arXiv", "Archivio online dove i ricercatori pubblicano studi "
     "prima che vengano controllati da altri esperti."),
    (r"fine[- ]?tuning", "Fine-tuning", "Specializzare un modello già addestrato su un compito o un tipo di "
     "testi particolare."),
    (r"\brag\b", "RAG", "Tecnica che fa consultare all'AI documenti specifici prima di rispondere, per "
     "ridurre gli errori."),
    (r"ragionament|reasoning", "Modelli che ragionano", "AI che prima di rispondere \"pensano\" in più "
     "passaggi: più lente, ma più brave in matematica e problemi complessi."),
    (r"chatbot", "Chatbot", "Un programma con cui si conversa per iscritto o a voce, come ChatGPT."),
    (r"\bcopyright|diritto d'autore", "Copyright", "Il diritto d'autore: molte cause riguardano l'uso di "
     "articoli, libri e immagini per addestrare le AI senza permesso."),
]
GLOSSARIO = [(re.compile(p, re.IGNORECASE), t, d) for p, t, d in GLOSSARIO]

ETICHETTE = [
    ("Fonte ufficiale", "La notizia viene dall'azienda o dall'ente interessato: affidabile sui fatti, ma è "
     "comunque autopromozione."),
    ("Confermata", "Riportata da almeno due testate giornalistiche affidabili."),
    ("Una fonte", "Riportata da una sola testata affidabile."),
    ("Da verificare", "Solo siti minori o discussioni online: prendila come una voce."),
    ("Appena lanciato", "Strumento presentato dal suo stesso creatore (es. Product Hunt): esiste e si può "
     "provare, ma nessuna testata l'ha ancora recensito."),
    ("Studio non revisionato", "Ricerca pubblicata prima del controllo di altri esperti."),
    ("Solo titoli", "Le fonti disponibili avevano solo il titolo: il riassunto si limita a quello, "
     "per i dettagli apri l'articolo."),
]

GIORNI_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
           "settembre", "ottobre", "novembre", "dicembre"]


def e(s):
    return html.escape(str(s or ""))


def data_italiana(d):
    return f"{GIORNI_IT[d.weekday()]} {d.day} {MESI_IT[d.month - 1]} {d.year}"


def data_breve(g):
    d = datetime.strptime(g, "%Y-%m-%d")
    return f"{d.day} {MESI_IT[d.month - 1][:3]}"


def chips(ids, per_id):
    out = []
    for i in ids:
        a = per_id.get(i)
        if not a:
            continue
        out.append(f'<a href="{e(a["link"])}" target="_blank" rel="noopener" title="{e(a["titolo"])}">'
                   f'{e(a["fonte"])} ↗</a>')
    return '<div class="src">' + "".join(out) + "</div>"


def badge(aff, solo_titoli=False):
    classe = {"Fonte ufficiale": "ok", "Confermata": "ok", "Da verificare": "warn",
              "Studio non revisionato": "warn"}.get(aff, "")
    out = f'<span class="rel {classe}">{e(aff)}</span>' if aff else ""
    if solo_titoli:
        out += ('<span class="rel soft" title="Le fonti avevano solo il titolo: per i dettagli apri '
                'l\'articolo">Solo titoli</span>')
    return out


def testo_della_giornata(r):
    pezzi = [r.get("titolo_giorno", ""), r.get("in_breve", "")]
    for d in r.get("da_provare", []):
        pezzi += [d.get("cosa", ""), d.get("a_cosa_serve", "")]
    for s in r.get("sezioni", []):
        for n in s.get("notizie", []):
            pezzi += [n.get("titolo", ""), n.get("riassunto", ""), n.get("perche_conta", "")]
    return " ".join(str(p) for p in pezzi)


def glossario_html(r, massimo=8):
    testo = testo_della_giornata(r)
    voci = [(t, d) for rx, t, d in GLOSSARIO if rx.search(testo)][:massimo]
    if not voci:
        return ""
    righe = "".join(f"<dt>{e(t)}</dt><dd>{e(d)}</dd>" for t, d in voci)
    return f'<h2>Parole da sapere</h2><section class="glossario"><dl>{righe}</dl></section>'


def testa(titolo, descrizione, url_pagina, prefisso):
    sito = indirizzo_sito()
    img = (sito + "anteprima.png") if sito else f"{prefisso}anteprima.png"
    return (f'<!doctype html><html lang="it"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f"<title>{e(titolo)}</title>"
            f'<meta name="description" content="{e(descrizione)}">'
            f'<meta name="theme-color" content="#c2410c">'
            f'<link rel="manifest" href="{prefisso}manifest.webmanifest">'
            f'<link rel="icon" type="image/png" href="{prefisso}icona-192.png">'
            f'<link rel="apple-touch-icon" href="{prefisso}apple-touch-icon.png">'
            f'<meta name="apple-mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-title" content="AI News">'
            f'<meta property="og:type" content="article"><meta property="og:site_name" content="AI News Digest">'
            f'<meta property="og:title" content="{e(titolo)}">'
            f'<meta property="og:description" content="{e(descrizione)}">'
            f'<meta property="og:image" content="{e(img)}">'
            f'<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta property="og:locale" content="it_IT">'
            f'<meta name="twitter:card" content="summary_large_image">'
            + (f'<meta property="og:url" content="{e(url_pagina)}">' if url_pagina else "")
            + f"<style>{CSS}</style></head>")


def piede(prefisso, dati_js):
    return (f"<script>const DIGEST={dati_js};const PREFISSO={json.dumps(prefisso)};{JS}</script>"
            "</body></html>")


def archivio_html(archivio, prefisso):
    if not archivio:
        return ""
    voci = "".join(f'<li><a href="{prefisso}giorni/{g}.html"><small>{e(data_breve(g))}</small>{e(t)}</a></li>'
                   for g, t in archivio[:60])
    return ('<h2>Archivio</h2>'
            '<input class="cerca" type="search" placeholder="🔎 Cerca nelle edizioni passate…" '
            'aria-label="Cerca nell\'archivio" oninput="cerca(this.value)" onfocus="caricaIndice()">'
            '<ul class="risultati" id="risultati" aria-live="polite"></ul>'
            f'<ul class="arch" id="arch">{voci}</ul>')


def legenda_html():
    righe = "".join(f"<dt>{badge(t) if t != 'Solo titoli' else badge('', True)}</dt><dd>{e(d)}</dd>"
                    for t, d in ETICHETTE)
    return f'<details><summary>Come leggere le etichette</summary><dl class="etichette">{righe}</dl></details>'


def pagina(dati, archivio, prefisso, settimana=None):
    r = dati["riassunto"]
    per_id = dati.get("articoli", {})
    stato = dati.get("fonti", [])
    modello = dati.get("modello", "")
    giorno = datetime.strptime(dati["giorno"], "%Y-%m-%d")
    data_it = data_italiana(giorno)
    quando = ""
    if dati.get("generato"):
        try:
            quando = f"Aggiornato alle {datetime.fromisoformat(dati['generato']).astimezone(TZ):%H:%M}"
        except ValueError:
            pass
    parti = [f'<p class="top">AI News Digest · {e(data_it)}</p>',
             f"<h1>{e(r.get('titolo_giorno'))}</h1>",
             f'<p class="when">{e(quando)}</p>' if quando else "",
             f'<p class="lead">{e(r.get("in_breve"))}</p>']
    audio = dati.get("audio")
    if audio and (DOCS / audio).exists():
        minuti = round((dati.get("durata") or 0) / 60)
        parti.append(f'<div class="listen"><span>🎧 Ascolta'
                     f'{f" · {minuti} min" if minuti else ""}</span>'
                     f'<audio controls preload="none" src="{prefisso}{e(audio)}"></audio></div>')
    parti.append('<div class="share">'
                 '<button type="button" onclick="condividi()">📤 Condividi</button>'
                 '<button type="button" onclick="copiaWhatsApp(this)">💬 Copia per WhatsApp</button>'
                 '<button type="button" onclick="window.print()">📄 Salva PDF</button></div>')
    if settimana:
        parti.append(f'<a class="week" href="{prefisso}settimana/{settimana[0]}.html">'
                     f'<small>📅 La settimana dell\'AI</small><b>{e(settimana[1])}</b> →</a>')
    nuovo_formato = r.get("formato") == 2
    nome_zona = "🧪 Strumenti nuovi" if nuovo_formato else "🧪 Da provare oggi"
    if not r.get("da_provare") and r.get("sezioni") and "non disponibile" not in str(r.get("in_breve")):
        parti.append(f'<section class="tryzone"><h2>{nome_zona}</h2><p class="sub">Oggi nessuno strumento nuovo '
                     'abbastanza concreto da consigliare: meglio niente che un suggerimento debole.</p></section>')
    if r.get("da_provare"):
        sotto = ("App e servizi AI appena usciti, da usare al lavoro o tutti i giorni" if nuovo_formato
                 else "Nuovi strumenti e possibilità emersi dalle notizie di oggi")
        parti.append(f'<section class="tryzone"><h2>{nome_zona}</h2><p class="sub">{sotto}</p>')
        for d in r["da_provare"]:
            etichette = [d.get("per_chi"), d.get("ambito"), d.get("costo"), d.get("difficolta")]
            if utile(d.get("piattaforma")):
                etichette.append(d["piattaforma"])
            if str(d.get("italiano", "")).strip().lower() in ("sì", "si"):
                etichette.append("In italiano")
            if utile(d.get("disponibilita")):
                etichette.append(d["disponibilita"])
            tag = (badge(d.get("affidabilita", ""), d.get("solo_titoli") and nuovo_formato)
                   + "".join(f'<span class="tag">{e(t)}</span>' for t in dict.fromkeys(etichette) if utile(t)))
            passi = d.get("come_iniziare") or d.get("come") or []
            if isinstance(passi, str):
                passi = [passi]
            passi = [x for x in passi if x and x != PASSO_ONESTO]
            if len(passi) == 1:
                come = f'<p class="why"><b>Come iniziare:</b> {e(passi[0])}</p>'
            elif passi:
                come = ('<p class="why"><b>Come iniziare</b></p><ol>'
                        + "".join(f"<li>{e(x)}</li>" for x in passi) + "</ol>")
            else:
                come = ""
            prova = (f'<a class="prova" href="{e(d["prova"])}" target="_blank" rel="noopener">Provalo ↗</a>'
                     if d.get("prova") else "")
            parti.append(f'<article class="try"><div class="tags">{tag}</div>'
                         f'<h3>{e(d.get("cosa"))}</h3><p>{e(d.get("a_cosa_serve"))}</p>'
                         f"{come}{prova}{chips(d['fonti'], per_id)}</article>")
        parti.append("</section>")
    k = 0
    for s in r["sezioni"]:
        parti.append(f"<h2>{e(s['titolo'])}</h2>")
        breve = s.get("breve")
        if breve:
            parti.append('<ul class="brevi">')
        for n in s["notizie"]:
            k += 1
            if breve:
                parti.append(f'<li id="n{k}"><b>{e(n["titolo"])}.</b> {e(n.get("riassunto"))} '
                             f"{chips(n['fonti'][:2], per_id)}</li>")
                continue
            perche = (f'<p class="why"><b>Perché conta:</b> {e(n["perche_conta"])}</p>'
                      if n.get("perche_conta") else "")
            if n.get("come_provarla"):
                perche += f'<p class="why"><b>Come provarla:</b> {e(n["come_provarla"])}</p>'
            extra = "".join(f'<span class="tag">{e(t)}</span>'
                            for t in (n.get("strumento"), n.get("disponibilita")) if utile(t))
            parti.append(f'<article id="n{k}"><div class="tags">{badge(n.get("affidabilita", ""), n.get("solo_titoli"))}'
                         f"{extra}</div><h3>{e(n['titolo'])}</h3><p>{e(n.get('riassunto'))}</p>"
                         f"{perche}{chips(n['fonti'], per_id)}</article>")
        if breve:
            parti.append("</ul>")
    parti.append(glossario_html(r))
    parti.append(archivio_html(archivio, prefisso))
    parti.append(legenda_html())
    ok = sum(1 for s in stato if s["ok"])
    righe = "".join(
        f"<li>{e(s['nome'])}: {s['presi']} articoli</li>" if s["ok"]
        else f"<li class='ko'>{e(s['nome'])}: non raggiungibile ({e(s.get('errore'))})</li>"
        for s in stato)
    completi = dati.get("testi_completi")
    extra = f" · testo completo per {completi}" if completi else ""
    parti.append(f"<details><summary>Fonti consultate: {ok}/{len(stato)} raggiungibili · "
                 f"{len(per_id)} articoli citati{extra} · {e(modello)}</summary><ul>{righe}</ul></details>")
    url_sito = indirizzo_sito()
    url_pagina = url_sito + (f"giorni/{dati['giorno']}.html" if prefisso else "")
    titolo_pag = f"AI News Digest · {giorno:%d/%m/%Y}"
    dati_js = json.dumps({"titolo": titolo_pag, "testo": testo_whatsapp(data_it, r, url_pagina),
                          "url": url_pagina}, ensure_ascii=False).replace("</", "<\\/")
    return (testa(f"{titolo_pag} — {r.get('titolo_giorno', '')}", r.get("in_breve", ""), url_pagina, prefisso)
            + f"<body><main>{''.join(parti)}</main>" + piede(prefisso, dati_js))


def pagina_settimana(w, archivio):
    prefisso = "../"
    fine = datetime.strptime(w["giorno"], "%Y-%m-%d")
    inizio = fine - timedelta(days=6)
    periodo = f"dal {inizio.day} {MESI_IT[inizio.month - 1]} al {fine.day} {MESI_IT[fine.month - 1]} {fine.year}"
    parti = [f'<p class="top">La settimana dell\'AI · {e(periodo)}</p>',
             f"<h1>{e(w.get('titolo'))}</h1>",
             f'<p class="lead">{e(w.get("in_breve"))}</p>',
             '<div class="share">'
             '<button type="button" onclick="condividi()">📤 Condividi</button>'
             '<button type="button" onclick="copiaWhatsApp(this)">💬 Copia per WhatsApp</button>'
             '<button type="button" onclick="window.print()">📄 Salva PDF</button></div>',
             ]
    nuovo = w.get("formato") == 2
    blocco_prove = []
    if w.get("da_provare"):
        titolo_prove = ("🧪 I migliori strumenti nuovi della settimana" if nuovo
                        else "🧪 Il meglio da provare della settimana")
        blocco_prove.append(f'<section class="tryzone"><h2>{titolo_prove}</h2>')
        for d in w["da_provare"]:
            blocco_prove.append(f'<article class="try"><h3>{e(d.get("cosa"))}</h3><p>{e(d.get("a_cosa_serve"))}</p>'
                                f'<div class="src"><a href="{prefisso}giorni/{e(d.get("giorno"))}.html">'
                                f'{e(data_breve(d.get("giorno")))} ↗</a></div></article>')
        blocco_prove.append("</section>")
    blocco_notizie = []
    if w.get("notizie"):
        blocco_notizie.append("<h2>Le novità che contano</h2>" if nuovo else "<h2>Le 5 notizie che contano</h2>")
    for i, n in enumerate(w.get("notizie", []), 1):
        link = "".join(f'<a href="{prefisso}giorni/{g}.html#n{k}">{e(data_breve(g))} ↗</a>'
                       for g, k in n.get("rif", []))
        perche = (f'<p class="why"><b>Perché conta:</b> {e(n.get("perche_conta"))}</p>'
                  if n.get("perche_conta") else "")
        blocco_notizie.append(f"<article><h3>{i}. {e(n.get('titolo'))}</h3><p>{e(n.get('racconto'))}</p>"
                              f'{perche}<div class="src">{link}</div></article>')
    parti += (blocco_prove + blocco_notizie) if nuovo else (blocco_notizie + blocco_prove)
    parti.append(f'<p class="meta"><a href="{prefisso}index.html">← Torna all\'edizione di oggi</a></p>')
    parti.append(archivio_html(archivio, prefisso))
    url_pagina = indirizzo_sito() + f"settimana/{w['giorno']}.html"
    righe = [f"📅 *La settimana dell'AI* · {periodo}", "", f"*{w.get('titolo', '')}*", w.get("in_breve", ""), ""]
    if w.get("formato") == 2 and w.get("da_provare"):
        righe.append("🧪 *Strumenti della settimana*")
        righe += [f"• *{d.get('cosa', '')}*: {d.get('a_cosa_serve', '')}" for d in w["da_provare"]]
        righe.append("")
    righe += [f"{i}. {n.get('titolo', '')}" for i, n in enumerate(w.get("notizie", []), 1)]
    if url_pagina:
        righe += ["", f"Leggi tutto: {url_pagina}"]
    dati_js = json.dumps({"titolo": "La settimana dell'AI", "testo": "\n".join(righe),
                          "url": url_pagina}, ensure_ascii=False).replace("</", "<\\/")
    return (testa(f"La settimana dell'AI — {w.get('titolo', '')}", w.get("in_breve", ""), url_pagina, prefisso)
            + f"<body><main>{''.join(parti)}</main>" + piede(prefisso, dati_js))


def indirizzo_sito():
    """Indirizzo GitHub Pages, ricavato dal nome del repository (disponibile dentro GitHub Actions)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return os.environ.get("SITO_URL", "")
    utente, nome = repo.split("/", 1)
    if nome.lower() == f"{utente.lower()}.github.io":
        return f"https://{utente.lower()}.github.io/"
    return f"https://{utente.lower()}.github.io/{nome}/"


def testo_whatsapp(data_it, r, url):
    """Versione testuale breve, con la formattazione di WhatsApp (*grassetto*, _corsivo_)."""
    righe = [f"🤖 *AI News Digest* · {data_it}", "", f"*{r.get('titolo_giorno', '')}*",
             r.get("in_breve", ""), ""]
    nuovo = r.get("formato") == 2
    if r.get("da_provare"):
        righe.append("🧪 *Strumenti nuovi*" if nuovo else "🧪 *Da provare oggi*")
        for d in r["da_provare"]:
            costo = f" ({d['costo']})" if nuovo and utile(d.get("costo")) else ""
            righe.append(f"• *{d.get('cosa', '')}*{costo}: {d.get('a_cosa_serve', '')}")
        righe.append("")
    if nuovo:
        novita = next((s for s in r.get("sezioni", []) if s.get("titolo") == SEZ_NOVITA), None)
        if novita:
            righe.append("✨ *Novità negli strumenti che già usi*")
            righe += [f"• {n.get('titolo', '')}" for n in novita["notizie"][:5]]
            righe.append("")
    else:
        principali = next((s for s in r.get("sezioni", []) if "principal" in s.get("titolo", "").lower()), None)
        if principali:
            righe.append("📰 *Le notizie principali*")
            for n in principali["notizie"][:5]:
                righe.append(f"• {n.get('titolo', '')}")
            righe.append("")
    if url:
        righe.append(f"Leggi tutto con le fonti: {url}")
    return "\n".join(righe).strip()


JS = """
function condividi(){
  const d={title:DIGEST.titolo,text:DIGEST.titolo,url:DIGEST.url||location.href};
  if(navigator.share){navigator.share(d).catch(()=>{});}
  else{copia(d.url,null,'Link copiato');}
}
function copiaWhatsApp(b){copia(DIGEST.testo,b,'✓ Copiato! Incollalo in WhatsApp');}
function copia(t,b,msg){
  const fatto=()=>{if(b){const o=b.textContent;b.textContent=msg;setTimeout(()=>b.textContent=o,2500);}else{alertino(msg);}};
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(fatto).catch(()=>vecchio(t,fatto));}
  else{vecchio(t,fatto);}
}
function vecchio(t,fatto){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';
  document.body.appendChild(a);a.select();try{document.execCommand('copy');fatto();}catch(e){}a.remove();}
function alertino(m){const d=document.createElement('div');d.className='toast';d.textContent=m;document.body.appendChild(d);setTimeout(()=>d.remove(),2500);}
let INDICE=null,attesa=null;
function caricaIndice(){
  if(INDICE||attesa)return attesa;
  attesa=fetch(PREFISSO+'indice.json',{cache:'no-cache'}).then(r=>r.json()).then(j=>{INDICE=j;return j;}).catch(()=>{INDICE=[];});
  return attesa;
}
function norm(s){return (s||'').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');}
function cerca(q){
  const ul=document.getElementById('risultati'),arch=document.getElementById('arch');
  q=norm(q.trim());
  if(q.length<2){ul.innerHTML='';arch.style.display='';return;}
  caricaIndice().then(()=>{
    const parole=q.split(/\\s+/),out=[];
    for(const g of INDICE||[]){for(const n of g.n){
      const t=norm(n[1]+' '+n[2]);
      if(parole.every(p=>t.includes(p)))out.push([g.g,n]);
    }}
    arch.style.display='none';
    ul.innerHTML=out.length?'':'<li>Nessun risultato.</li>';
    for(const [g,n] of out.slice(0,40)){
      const li=document.createElement('li'),a=document.createElement('a');
      a.href=PREFISSO+'giorni/'+g+'.html'+(n[0]?'#n'+n[0]:'');
      const s=document.createElement('small');s.textContent=g.split('-').reverse().join('/');
      a.append(s,document.createTextNode(n[1]));li.append(a);ul.append(li);
    }
  });
}
if('serviceWorker' in navigator){addEventListener('load',()=>navigator.serviceWorker.register(PREFISSO+'sw.js').catch(()=>{}));}
"""


# ---------------------------------------------------------------- archivio e pagine

def giorni_salvati():
    return sorted((p for p in DATA.glob("*.json") if re.fullmatch(r"\d{4}-\d\d-\d\d", p.stem)),
                  key=lambda p: p.stem)


def leggi_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as ex:
        print(f"  ✗ {p.name} illeggibile: {ex}", file=sys.stderr)
        return None


def rigenera_tutto():
    """Ricrea tutte le pagine a partire dai dati salvati (così ogni modifica grafica vale anche per l'archivio)."""
    for d in (DATA, GIORNI, SETTIMANE):
        d.mkdir(parents=True, exist_ok=True)
    edizioni = [x for x in (leggi_json(p) for p in giorni_salvati()) if x and x.get("riassunto")]
    if not edizioni:
        print("Nessuna edizione salvata: niente da pubblicare.", file=sys.stderr)
        return
    archivio = [(x["giorno"], x["riassunto"].get("titolo_giorno", "")) for x in reversed(edizioni)]

    indice = []
    for x in reversed(edizioni):
        voci, k = [], 0
        for s in x["riassunto"].get("sezioni", []):
            for n in s.get("notizie", []):
                k += 1
                voci.append([k, n.get("titolo", ""), (n.get("riassunto") or "")[:220]])
        for d in x["riassunto"].get("da_provare", []):
            voci.append([0, "🧪 " + d.get("cosa", ""), (d.get("a_cosa_serve") or "")[:220]])
        indice.append({"g": x["giorno"], "t": x["riassunto"].get("titolo_giorno", ""), "n": voci})
    (DOCS / "indice.json").write_text(json.dumps(indice, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")

    settimane = [x for x in (leggi_json(p) for p in sorted(SETTIMANE.glob("*.json"))) if x]
    for w in settimane:
        (SETTIMANE / f"{w['giorno']}.html").write_text(pagina_settimana(w, archivio), encoding="utf-8")

    ultima = edizioni[-1]
    banner = None
    if settimane:
        w = settimane[-1]
        distanza = (datetime.strptime(ultima["giorno"], "%Y-%m-%d") - datetime.strptime(w["giorno"], "%Y-%m-%d")).days
        if 0 <= distanza <= 6:
            banner = (w["giorno"], w.get("titolo", ""))
    for x in edizioni:
        (GIORNI / f"{x['giorno']}.html").write_text(pagina(x, archivio, "../"), encoding="utf-8")
    (DOCS / "index.html").write_text(pagina(ultima, archivio, "", banner), encoding="utf-8")
    print(f"Pagine aggiornate: {len(edizioni)} edizioni, {len(settimane)} settimanali.")


# ---------------------------------------------------------------- la settimana dell'AI (domenica)

ISTRUZIONI_SETTIMANA = """Sei il redattore di "AI News Digest", una guida in ITALIANO agli strumenti di intelligenza
artificiale per persone NON programmatrici, da usare al lavoro e nella vita di tutti i giorni.
Ricevi quanto pubblicato negli ultimi 7 giorni: strumenti nuovi (codice [AAAA-MM-GG#provare-n]) e novità
o notizie (codice [AAAA-MM-GG#n]).
1. Scegli i 5 MIGLIORI strumenti nuovi della settimana (o meno, se non sono validi): preferisci quelli
   gratuiti o con prova gratuita, utili a molte persone, disponibili in Italia.
2. Scegli fino a 5 novità più utili negli strumenti già diffusi (ChatGPT, Gemini, Claude, Copilot...).
   Se una novità è stata raccontata in più giorni, uniscila in una sola voce citando tutti i codici.
Usa SOLO le informazioni presenti nel testo ricevuto, senza aggiungere nulla di tuo. Mai voci "Da verificare".
Scrivi sempre "AI", mai "IA".

Rispondi SOLO con JSON valido:
{"titolo": "titolo breve della settimana",
 "in_breve": "2-3 frasi: le cose più utili scoperte questa settimana",
 "da_provare": [{"cosa": "...", "a_cosa_serve": "1-2 frasi", "rif": "2026-10-06#provare-1"}],
 "notizie": [{"titolo": "...", "racconto": "2-3 frasi", "perche_conta": "1 frase concreta",
              "rif": ["2026-10-05#3", "2026-10-07#1"]}]}"""


def costruisci_settimana(chiave, fine):
    """Crea docs/settimana/<fine>.json con le 5 notizie della settimana che si chiude in <fine>."""
    inizio = (datetime.strptime(fine, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    edizioni = [x for x in (leggi_json(p) for p in giorni_salvati() if inizio <= p.stem <= fine) if x]
    edizioni = [x for x in edizioni if "senza AI" not in str(x.get("modello", ""))]
    if len(edizioni) < 3:
        print(f"Settimana: solo {len(edizioni)} edizioni utili, salto.")
        return False
    righe, validi, prove = [], set(), {}
    for x in edizioni:
        k = 0
        for s in x["riassunto"].get("sezioni", []):
            for n in s.get("notizie", []):
                k += 1
                cod = f"{x['giorno']}#{k}"
                validi.add(cod)
                righe.append(f"[{cod}] ({n.get('affidabilita', '')}) {n.get('titolo', '')} — "
                             f"{n.get('riassunto', '')}")
        for j, d in enumerate(x["riassunto"].get("da_provare", []), 1):
            cod = f"{x['giorno']}#provare-{j}"
            prove[cod] = x["giorno"]
            costo = f" ({d['costo']})" if utile(d.get("costo")) else ""
            righe.append(f"[{cod}] STRUMENTO: {d.get('cosa', '')}{costo} — {d.get('a_cosa_serve', '')}")
    w, modello = chiama_gemini(ISTRUZIONI_SETTIMANA, "NOTIZIE DELLA SETTIMANA:\n" + "\n".join(righe), chiave)
    notizie = []
    for n in w.get("notizie", []):
        rif = [c for c in dict.fromkeys(n.get("rif", [])) if c in validi]
        if rif and n.get("titolo"):
            n["rif"] = [(c.split("#")[0], int(c.split("#")[1])) for c in rif]
            notizie.append(n)
    da_provare = []
    for d in w.get("da_provare", [])[:5]:
        if d.get("rif") in prove and d.get("cosa"):
            d["giorno"] = prove[d["rif"]]
            da_provare.append(d)
    if len(notizie) + len(da_provare) < 3:
        raise RuntimeError("riassunto settimanale troppo povero")
    out = {"giorno": fine, "formato": 2, "titolo": w.get("titolo", "La settimana dell'AI"), "in_breve": w.get("in_breve", ""),
           "notizie": notizie[:5], "da_provare": da_provare, "modello": modello,
           "generato": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    SETTIMANE.mkdir(parents=True, exist_ok=True)
    (SETTIMANE / f"{fine}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ✓ settimana {inizio} → {fine}: {len(out['notizie'])} notizie")
    return True


# ---------------------------------------------------------------- avvisi

def avvisa(msg):
    print("⚠️  " + msg, file=sys.stderr)
    AVVISI.append(msg)


def scrivi_avvisi():
    f = os.environ.get("AVVISI_FILE")
    if f and AVVISI:
        with open(f, "a", encoding="utf-8") as out:
            out.write("".join(f"- {m}\n" for m in AVVISI))


# ---------------------------------------------------------------- main

def main():
    import locale
    for loc in ("it_IT.UTF-8", "it_IT.utf8", "it_IT"):
        try:
            locale.setlocale(locale.LC_TIME, loc)
            break
        except locale.Error:
            pass

    chiave = os.environ.get("GEMINI_API_KEY", "").strip()
    if "--pagine" in sys.argv:          # solo ricostruzione delle pagine (es. dopo aver creato l'audio)
        rigenera_tutto()
        return
    if "--settimana" in sys.argv:       # forza l'edizione settimanale che termina oggi
        costruisci_settimana(chiave, datetime.now(TZ).strftime("%Y-%m-%d"))
        rigenera_tutto()
        return

    config = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    fonti = config["fonti"]
    limite = datetime.now(timezone.utc) - timedelta(hours=ORE_FINESTRA)
    print(f"Leggo {len(fonti)} fonti (ultime {ORE_FINESTRA} ore)…")
    articoli, stato = raccogli(fonti, limite, config.get("editori_esclusi", []))
    irraggiungibili = [s["nome"] for s in stato if not s["ok"]]
    if len(irraggiungibili) > len(stato) / 3:
        avvisa(f"{len(irraggiungibili)} fonti su {len(stato)} non raggiungibili: {', '.join(irraggiungibili)}.")
    if not articoli:
        avvisa("Nessun articolo trovato: il sito non è stato aggiornato.")
        scrivi_avvisi()
        sys.exit(1)

    oggi = datetime.now(TZ)
    giorno = oggi.strftime("%Y-%m-%d")

    # Niente doppioni: togliamo gli articoli già citati nelle ultime due settimane e diciamo a Gemini
    # quali strumenti e novità sono già stati segnalati (così uno strumento non torna ogni giorno).
    GIORNI_MEMORIA = 14
    soglia = (oggi - timedelta(days=GIORNI_MEMORIA)).strftime("%Y-%m-%d")
    recenti = [x for x in (leggi_json(p) for p in giorni_salvati() if soglia <= p.stem < giorno) if x]
    gia_pubblicate = []
    if recenti:
        chiave_t = lambda t: re.sub(r"\W+", "", (t or "").lower())[:80]
        vecchi_link, vecchi_titoli = set(), set()
        for x in recenti:
            for a in x.get("articoli", {}).values():
                vecchi_link.add(a.get("link"))
                vecchi_titoli.add(chiave_t(a.get("titolo")))
        prima = len(articoli)
        articoli = [a for a in articoli
                    if a["link"] not in vecchi_link and chiave_t(a["titolo"]) not in vecchi_titoli]
        for x in recenti:
            r_ = x.get("riassunto", {})
            gia_pubblicate += [d.get("cosa", "") for d in r_.get("da_provare", [])]
            gia_pubblicate += [n.get("titolo", "") for s in r_.get("sezioni", []) for n in s.get("notizie", [])]
        gia_pubblicate = [t for t in dict.fromkeys(gia_pubblicate) if t][-150:]
        print(f"Tolti {prima - len(articoli)} articoli già usati negli ultimi {GIORNI_MEMORIA} giorni.")
        if not articoli:
            avvisa("Tutti gli articoli erano già stati pubblicati: il sito non è stato aggiornato.")
            scrivi_avvisi()
            sys.exit(1)

    completi = arricchisci(articoli)
    candidati = sum(1 for a in articoli if a.get("tipo") in ("ufficiale", "testata", "minore"))
    if candidati >= 10 and completi == 0:
        avvisa("Non è stato possibile scaricare il testo di nessun articolo: il riassunto si basa solo sui titoli.")
    for a in articoli:
        a.setdefault("solo_titolo", not a.get("testo"))
    per_id = {a["id"]: a for a in articoli}

    modello = "senza AI"
    if chiave:
        try:
            riassunto, modello = chiedi_a_gemini(articoli, chiave, gia_pubblicate)
            riassunto = verifica(riassunto, per_id)
            if not (riassunto["sezioni"] or riassunto.get("da_provare")):
                raise RuntimeError("riassunto vuoto")
        except Exception as ex:  # qualunque problema: si pubblica comunque la versione semplice
            avvisa(f"Gemini non disponibile ({str(ex)[:200]}): pubblicata la versione senza riassunto.")
            riassunto, modello = senza_ai(articoli), "senza AI (Gemini non disponibile)"
    else:
        avvisa("GEMINI_API_KEY non impostata: pubblicata la versione senza riassunto.")
        riassunto = senza_ai(articoli)

    for d in (DATA, GIORNI):
        d.mkdir(parents=True, exist_ok=True)
    usati = {i for s in riassunto["sezioni"] for n in s["notizie"] for i in n["fonti"]}
    usati |= {i for d in riassunto.get("da_provare", []) for i in d["fonti"]}
    salvati = {i: {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in per_id[i].items()}
               for i in usati}
    dati = {"giorno": giorno, "generato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "modello": modello, "testi_completi": completi, "riassunto": riassunto,
            "articoli": salvati, "fonti": stato}
    (DATA / f"{giorno}.json").write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")

    if oggi.weekday() == 6 or os.environ.get("SETTIMANA") == "1":
        if chiave:
            try:
                costruisci_settimana(chiave, giorno)
            except Exception as ex:
                avvisa(f"Edizione della settimana non creata: {str(ex)[:200]}")

    rigenera_tutto()
    scrivi_avvisi()
    print(f"Fatto: edizione del {giorno}.")


if __name__ == "__main__":
    main()
