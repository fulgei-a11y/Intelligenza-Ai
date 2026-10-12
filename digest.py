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
# Orari degli aggiornamenti automatici in ora UTC: DEVONO coincidere con i "cron" di .github/workflows/digest.yml.
# 05:17 e 16:17 UTC = 7:17 e 18:17 con l'ora legale, 6:17 e 17:17 con l'ora solare.
ORARI_UTC = ["05:17", "16:17"]
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
_vuole_browser = set()


def scarica(url: str, timeout: int = 25) -> bytes:
    """Scarica un feed. Aspetta tra due richieste allo stesso sito e riprova se il sito
    risponde 'troppe richieste' (429) o è temporaneamente non disponibile."""
    host = urllib.parse.urlparse(url).netloc
    for tentativo in range(3):
        attesa = 6 - (time.time() - _ultimo_accesso.get(host, 0))
        if attesa > 0:
            time.sleep(attesa)
        _ultimo_accesso[host] = time.time()
        ua = UA_BROWSER if tentativo and url in _vuole_browser else UA
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 15 * tentativo) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 403 and url not in _vuole_browser and tentativo < 2:
                _vuole_browser.add(url)    # alcuni siti (es. GIJN) rifiutano i programmi automatici
                continue
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
Vuole soprattutto restare un passo avanti: capire dove sta andando l'AI, scoprire le novità che contano
in TUTTI gli ambiti (scrittura, foto e video, voce, documenti, organizzazione, dati, studio, creatività...)
e poterle sfruttare nel lavoro e nel personale. Non restringere per tipo di lavoro.
Il lettore usa un telefono ANDROID e un PC WINDOWS: per lui conta sapere DOVE si usa ogni strumento.
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
2. Prezzo, lingua italiana, disponibilità in Italia e piattaforme: scrivili SOLO se un articolo li dice.
   Altrimenti usa "Non indicato" (per le piattaforme: lista vuota []). Per le piattaforme usa SOLO questi
   nomi: "Browser" (sito web o estensione del browser), "Android", "iPhone", "Windows", "Mac", "Linux".
   Un'app per iPhone NON è automaticamente anche per Android, e un'app per Mac non è anche per Windows.
3. "come_iniziare": 2-4 passi concreti SOLO se un articolo li descrive davvero; altrimenti lista vuota [].
   MAI frasi vuote come "apri l'articolo".
4. MAI riportare accuse (reati, frodi, scandali) contro persone o aziende.
5. Se ricevi l'elenco "GIÀ SEGNALATI DI RECENTE", non riproporre quegli strumenti e quelle novità, a meno
   che oggi ci sia uno sviluppo nuovo e concreto (es. arriva in Italia, diventa gratis): in quel caso
   racconta SOLO la novità.
6. "titolo_giorno" e "presentazione" possono nominare SOLO strumenti o novità che hai messo in una delle liste
   (strumenti_nuovi, osint, novita_strumenti, in_breve). Mai citare qualcosa che non ha la sua scheda.
   Elenca in "citati" i nomi che hai usato.
7. Scrivi chiaro, frasi brevi, niente gergo; spiega ogni termine tecnico in poche parole.
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
   ORDINE: metti PRIMA gli strumenti che si usano nel Browser o su Android (il lettore li prova subito),
   poi quelli per Windows, poi gli altri. Gli strumenti SOLO per Mac o SOLO per iPhone riportali comunque
   se sono davvero interessanti (fanno capire dove va l'AI), ma in fondo: la pagina li segnerà come tali.
   Preferisci ciò che è gratuito o ha una prova gratuita, funziona in italiano o è disponibile in Italia.
   AI SUL DISPOSITIVO: dai particolare risalto alle AI che funzionano sul telefono o sul computer senza
   mandare i dati online (locali, offline, private, come KernelAI): il lettore segue questa tendenza.
   Per queste metti "sul_dispositivo": true (solo se le fonti dicono che funziona in locale/offline).
   "come_sfruttarla": UNA frase con un uso pratico concreto, anche nel lavoro (es. "Riassumere i verbali
   delle riunioni senza caricarli online"). Scrivila solo se ciò che dicono le fonti permette un esempio
   sensato; altrimenti stringa vuota "". Niente frasi generiche come "aumenta la produttività".
   Se oggi non c'è nulla di valido, lascia la lista vuota.
B) "novita_strumenti" — "Novità negli strumenti che già usi" (da 0 a 5)
   Nuove funzioni concrete di ChatGPT, Gemini, Claude, Copilot, Meta AI, Perplexity, app Google,
   Microsoft, Apple, Samsung e simili. Spiega cosa cambia per chi le usa e, se le fonti lo dicono,
   come provarla e se è già disponibile in Italia.
   ESCLUDI: modelli per soli sviluppatori, prodotti per aziende (Google Cloud, Azure, AWS, "Enterprise",
   piani aziendali, agenti per i sistemi aziendali), novità riservate ai clienti business.
   Vanno bene i piani per privati (anche a pagamento) e gli strumenti per liberi professionisti.
D) "osint" — "Strumenti OSINT" (da 0 a 4): strumenti NUOVI o aggiornati per indagini su fonti aperte, come
   quelli usati da giornalisti investigativi, fact-checker e analisti. Solo strumenti reali e funzionanti,
   usabili nel BROWSER (sito o estensione) o su WINDOWS; se le fonti non dicono la piattaforma va bene lo
   stesso (lascia "piattaforme" vuoto). Ambiti ammessi: verifica di foto e video,
   geolocalizzazione di immagini, metadati di file, archivi web, domini e siti, aziende e registri pubblici,
   navi e aerei, immagini satellitari, analisi di documenti, mappe e dati aperti, e anche PERSONE E ACCOUNT
   su fonti pubbliche (username e profili social, email e numeri di telefono, presenza online, verifica di
   identità e antifrode, controllo se un'email compare in una fuga di dati, soggetti nei registri pubblici).
   Per questi ultimi usa "uso": "Persone e account".
   QUALITÀ: preferisci gli strumenti segnalati o usati da Bellingcat, GIJN, fact-checker e giornalisti
   investigativi, e quelli gratuiti o con una versione gratuita. NON sono strumenti da segnalare: piattaforme
   aziendali di "threat intelligence" o cybersicurezza vendute a grandi aziende, corsi, eventi, webinar,
   certificazioni, offerte di lavoro, notizie su attacchi hacker. Un'inchiesta che USA uno strumento è valida
   solo se l'articolo spiega lo strumento in modo che il lettore possa usarlo anche lui.
   ESCLUDI SEMPRE, senza eccezioni: spyware e app per controllare di nascosto un telefono o un account;
   archivi di dati rubati o password trafugate; riconoscimento facciale per identificare persone;
   localizzazione in tempo reale di una persona o di un telefono; strumenti di intrusione o hacking.
   Per ognuno: "uso" (2-4 parole, es. "Verifica immagini", "Geolocalizzazione", "Aziende e registri"),
   "costo" (Gratis / Gratis con limiti / A pagamento / Non indicato), "piattaforme", "come_sfruttarla"
   (un esempio di indagine lecita, es. "Verificare se una foto virale è davvero di oggi"). Se uno strumento
   si usa solo da riga di comando, scrivi "difficolta": "Per esperti". Se oggi non c'è nulla, lista vuota.
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
  "presentazione": "2-3 frasi: lo strumento più interessante di oggi, cosa dice di dove va l'AI e come sfruttarlo. Niente frasi sulla rassegna stessa",
  "citati": ["nomi esatti degli strumenti o servizi nominati in titolo_giorno e presentazione"],
  "strumenti_nuovi": [
    {"cosa": "nome dello strumento", "a_cosa_serve": "1-2 frasi con un esempio concreto",
     "per_chi": "Lavoro / Tutti i giorni / Lavoro e tutti i giorni / Studio",
     "ambito": "2-3 parole, es. Scrittura, Foto e video, Organizzazione, Ricerca, Casa, Salute, Viaggi",
     "come_iniziare": ["passo 1", "passo 2"],
     "costo": "Gratis / Gratis con limiti / A pagamento / Prova gratuita / Non indicato",
     "piattaforme": ["Browser", "Android"],
     "sul_dispositivo": false,
     "come_sfruttarla": "1 frase con un uso pratico, anche al lavoro, oppure stringa vuota",
     "italiano": "Sì / No / Non indicato",
     "difficolta": "Facile / Media", "fonti": ["a3"]}
  ],
  "novita_strumenti": [
    {"strumento": "ChatGPT", "titolo": "...", "cosa_cambia": "2-3 frasi pratiche",
     "come_provarla": "1 frase, solo se le fonti lo dicono, altrimenti stringa vuota",
     "disponibilita": "Disponibile ora / In arrivo / Non in Italia / Non indicato", "fonti": ["a5"]}
  ],
  "osint": [
    {"cosa": "nome", "a_cosa_serve": "1-2 frasi", "uso": "Verifica immagini", "piattaforme": ["Browser"],
     "costo": "Gratis", "difficolta": "Facile / Media / Per esperti", "come_sfruttarla": "1 frase", "fonti": ["a7"]}
  ],
  "in_breve": [
    {"titolo": "...", "frase": "1 frase", "fonti": ["a9"]}
  ]
}
Ogni elemento deve avere almeno un id valido in "fonti"."""


AVVISO_AGGIORNAMENTO = """QUESTO È UN AGGIORNAMENTO DELLA GIORNATA: il lettore ha già letto l'edizione del mattino.
Riporta SOLO strumenti e novità davvero nuovi, assenti dall'elenco "GIÀ SEGNALATI DI RECENTE".
Meglio niente che un doppione: se non c'è nulla di nuovo e utile, restituisci liste vuote.
"titolo_giorno" e "presentazione" devono riguardare SOLO le novità di questo aggiornamento.

"""


def chiedi_a_gemini(articoli, chiave, gia_pubblicate=(), aggiornamento=False):
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
    if aggiornamento:
        testo = AVVISO_AGGIORNAMENTO + testo
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
            "citati": r.get("citati", []) or [],
            "da_provare": r.get("strumenti_nuovi", []) or [], "osint": r.get("osint", []) or [],
            "sezioni": sezioni, "formato": 2}


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


PIATTAFORME = ["Browser", "Android", "Windows", "iPhone", "Mac", "Linux"]
USABILI = ("Browser", "Android", "Windows")         # quelle che il lettore può provare subito
SINONIMI_PIATTAFORMA = [
    (r"browser|\bweb\b|sito|online|estension|extension|chrome|edge|firefox", "Browser"),
    (r"android|google play", "Android"), (r"windows|\bpc\b", "Windows"),
    (r"iphone|\bios\b|ipad|app store", "iPhone"), (r"\bmac|macos|osx", "Mac"), (r"linux", "Linux"),
]


def piattaforme_di(d):
    """Lista ordinata delle piattaforme di uno strumento (anche dal vecchio campo testuale 'piattaforma')."""
    grezzo = d.get("piattaforme")
    if grezzo is None:
        grezzo = d.get("piattaforma", "")
    voci = grezzo if isinstance(grezzo, list) else re.split(r"[,/;]| e ", str(grezzo))
    trovate = set()
    for v in voci:
        for rx, nome in SINONIMI_PIATTAFORMA:
            if re.search(rx, str(v), re.IGNORECASE):
                trovate.add(nome)
    return [p for p in PIATTAFORME if p in trovate]


def etichette_piattaforma(d):
    """['✓ Browser', '✓ Android', 'iPhone'] oppure ['Solo Mac'] se il lettore non può usarlo."""
    pp = piattaforme_di(d)
    if not pp:
        return []
    if not any(p in USABILI for p in pp):
        return ["Solo " + " e ".join(pp)]
    return [("✓ " + p) if p in USABILI else p for p in pp]


def ordine_strumento(d):
    pp = piattaforme_di(d)
    if "Browser" in pp or "Android" in pp:
        return 0
    if "Windows" in pp:
        return 1
    if not pp:
        return 2                                     # piattaforma non indicata
    return 3                                         # solo Mac / iPhone / Linux


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
    proposti = [d.get("cosa", "") for d in riassunto.get("da_provare", []) + (riassunto.get("osint") or [])]
    proposti += [n.get("titolo", "") for s in riassunto.get("sezioni", []) for n in s.get("notizie", [])]
    riassunto = _verifica(riassunto, per_id)
    return controlla_presentazione(riassunto, proposti)


SIGLE_NOTE = {"AI", "IA", "OSINT", "GDPR", "API", "PDF", "USA", "UE", "EU", "LLM", "APP", "WEB", "PC", "IOS", "GPS",
              "SMS", "OCR", "FAQ", "CEO", "ANSA", "NASA", "NATO", "ONU", "AGI", "RAG", "GPT", "VPN"}


def _norm(t):
    return re.sub(r"\W+", "", (t or "").lower())


def controlla_presentazione(riassunto, proposti=()):
    """Il titolo e la presentazione devono parlare solo di ciò che ha una scheda. Se nominano qualcosa che è stato
    scartato o che non c'è (es. «OSIRIS AI LIVE» citato senza scheda), vengono riscritti a partire dalle schede."""
    if riassunto.get("formato") != 2:
        return riassunto
    tools = riassunto.get("da_provare", []) + riassunto.get("osint", [])
    notizie = [n for s in riassunto.get("sezioni", []) for n in s.get("notizie", [])]
    tenuti = [d.get("cosa", "") for d in tools] + [n.get("titolo", "") for n in notizie] + \
             [n.get("strumento", "") for n in notizie]
    testo_tenuti = _norm(" ".join(tenuti + [d.get("a_cosa_serve", "") for d in tools] +
                                  [n.get("riassunto", "") for n in notizie]))
    vetrina = f"{riassunto.get('titolo_giorno', '')} {riassunto.get('in_breve', '')}"
    problemi = [p for p in proposti if p and _norm(p) not in _norm(" ".join(tenuti)) and _norm(p) in _norm(vetrina)]
    problemi += [c for c in riassunto.get("citati", []) or [] if _norm(c) and _norm(c) not in testo_tenuti]
    problemi += [m for m in re.findall(r"\b[A-Z][A-Z0-9]{3,}(?:\s+[A-Z][A-Z0-9]+)*\b", vetrina)
                 if not set(m.split()) <= SIGLE_NOTE and _norm(m) not in testo_tenuti]
    if not problemi:
        return riassunto
    print(f"  ✎ presentazione riscritta: nominava {', '.join(dict.fromkeys(problemi))} senza scheda")
    nomi = [d.get("cosa", "") for d in tools] or [n.get("titolo", "") for n in notizie]
    if not nomi:
        riassunto["titolo_giorno"], riassunto["in_breve"] = "Le novità AI di oggi", ""
        return riassunto
    riassunto["titolo_giorno"] = ("Novità di oggi: " + ", ".join(nomi[:2])
                                  + (f" e altri {len(nomi) - 2}" if len(nomi) > 2 else ""))
    frasi = []
    for d in tools[:2]:
        primo = re.split(r"(?<=[.!?])\s", d.get("a_cosa_serve", "").strip())[0]
        frasi.append(f"{d.get('cosa', '')}: {primo}" if primo else d.get("cosa", ""))
    if not frasi:
        frasi = [f"{n.get('titolo', '')}." for n in notizie[:2]]
    riassunto["in_breve"] = " ".join(f if f.endswith((".", "!", "?")) else f + "." for f in frasi)
    return riassunto


def _verifica(riassunto, per_id):
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

    prove = [x for x in (verifica_strumento(d, per_id) for d in riassunto.get("da_provare", [])) if x]
    prove.sort(key=ordine_strumento)        # prima Browser e Android, poi Windows, poi il resto (ordine stabile)
    riassunto["da_provare"] = prove[:6]

    # OSINT: solo strumenti veri, usabili nel browser o su Windows, mai per spiare o schedare persone.
    osint = []
    for d in riassunto.get("osint", []) or []:
        testo = f"{d.get('cosa', '')} {d.get('a_cosa_serve', '')} {d.get('uso', '')}"
        if OSINT_VIETATI.search(testo):
            print(f"  ⊘ OSINT scartato (uso su persone/intrusivo): {d.get('cosa')}")
            continue
        d = verifica_strumento(d, per_id)
        if not d:
            continue
        if d["piattaforme"] and not any(p in ("Browser", "Windows") for p in d["piattaforme"]):
            print(f"  ⊘ OSINT scartato (non usabile nel browser o su Windows): {d.get('cosa')}")
            continue                       # se la piattaforma non è indicata lo teniamo: quasi sempre è un sito web
        if not utile(d.get("costo")):
            d["costo"] = "Costo non indicato"
        osint.append(d)
    osint.sort(key=lambda d: 0 if "Browser" in d["piattaforme"] else 1 if d["piattaforme"] else 2)
    riassunto["osint"] = osint[:4]
    return riassunto


def verifica_strumento(d, per_id):
    """Controlli comuni a ogni scheda strumento. Restituisce la scheda pulita, o None se va scartata."""
    ids = pulisci_fonti(d.get("fonti", []), per_id)
    if not (ids and d.get("cosa")):
        return None
    if all(per_id[i].get("tipo") == "ricerca" for i in ids):
        return None                        # la ricerca non è uno strumento da provare
    if ACCUSE.search(f"{d.get('cosa', '')} {d.get('a_cosa_serve', '')}") and \
            {per_id[i].get("tipo") for i in ids} <= {"community", "minore", "vetrina"}:
        return None
    d["affidabilita"] = affidabilita(ids, per_id)
    passi = d.get("come_iniziare") or []
    if isinstance(passi, str):
        passi = [passi]
    passi = [p for p in passi if p and "apri l'articolo" not in p.lower()]
    if all(per_id[i].get("solo_titolo") for i in ids):
        passi = []                         # senza testo non sappiamo come si attiva: niente passi dedotti
        d["solo_titoli"] = True
        # Dal solo titolo non si ricavano prezzo, lingua e piattaforma, salvo che il titolo li dica.
        titoli = " ".join(per_id[i]["titolo"] for i in ids).lower()
        if not re.search(r"\bfree\b|gratis|gratuit", titoli):
            d["costo"] = "Non indicato"
        if "ital" not in titoli:
            d["italiano"] = "Non indicato"
        if not re.search(r"iphone|ios|android|mac|windows|chrome|web|browser", titoli):
            d["piattaforme"] = []
        d["sul_dispositivo"] = bool(d.get("sul_dispositivo")) and bool(
            re.search(r"offline|on-device|local|privat|senza internet|sul (tuo )?(telefono|dispositivo)", titoli))
    d["come_iniziare"] = passi[:4]
    d["piattaforme"] = piattaforme_di(d)
    d["sul_dispositivo"] = bool(d.get("sul_dispositivo"))
    sfrutta = str(d.get("come_sfruttarla") or "").strip()
    generica = re.search(r"produttivit|efficienz|risparmia(re)? tempo", sfrutta, re.IGNORECASE)
    if len(sfrutta) < 15 or (generica and len(sfrutta) < 60):
        sfrutta = ""                        # niente frasi vuote o generiche
    d["come_sfruttarla"] = sfrutta
    d["prova"] = link_prova(ids, per_id)
    d["fonti"] = ids[:MAX_LINK]
    return d


# Gli unici strumenti OSINT mai proposti: spiare di nascosto, dati rubati, riconoscimento facciale di persone,
# localizzazione in tempo reale di persone o telefoni, intrusione. Tutto il resto (anche persone e account
# su fonti pubbliche) è ammesso, con l'avviso sull'uso lecito.
OSINT_VIETATI = re.compile(
    r"stalker|spyware|spy app|app spia|spia(re)? (il|la|un|una|partner|telefono|whatsapp)|monitor(are|aggio)? di nascosto|"
    r"keylogger|malware|phishing|exploit|hack(ing|erare)? (account|social|profil)|"
    r"face.?search|ricerca (per|del) volto|riconoscimento facciale|facial recognition|reverse face|"
    r"(real.?time|in tempo reale|live) (location|tracking|posizione)|"
    r"(track|locate|trace) (a )?(phone|person|someone)|"
    r"(traccia|localizza)(re)? (una persona|il telefono|un telefono|un numero|qualcuno)|"
    r"combolist|credential dump|password dump|stolen (data|credentials|passwords)|dati rubati|"
    r"leak(ed)? (database|credentials|passwords)|doxx",
    re.IGNORECASE)


def senza_ai(articoli):
    """Versione di riserva: titoli raggruppati per categoria, senza riassunto."""
    ordine = ["Strumenti", "OSINT", "Laboratori", "Testate tech", "Italia", "Community"]
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
.dove{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 8px;font:13px system-ui,sans-serif;color:var(--muted)}
.dove b{font-weight:600;margin-right:2px}
.pf{font:600 12px system-ui,sans-serif;border-radius:6px;padding:3px 8px;background:var(--chip);color:var(--ink)}
.pf.si{background:#dcfce7;color:#15803d}.pf.solo{background:#f3f4f6;color:#6b7280;border:1px dashed #9ca3af}
.pf.locale{background:#e0e7ff;color:#3730a3}
@media (prefers-color-scheme:dark){.pf.si{background:#14532d;color:#86efac}.pf.solo{background:transparent;color:#9c9a93}
.pf.locale{background:#1e1b4b;color:#c7d2fe}}
.osint{background:var(--chip);border-radius:16px;padding:4px 16px 12px;margin:28px -4px 0;border-left:4px solid #0f766e}
.osint h2{color:#0f766e;border-bottom:none;margin:18px 0 0;font-size:16px}.osint .try{border-left-color:#0f766e}
@media (prefers-color-scheme:dark){.osint h2{color:#5eead4}.osint{border-left-color:#5eead4}.osint .try{border-left-color:#5eead4}}
.metodo{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 18px 10px;margin:18px 0}
.metodo h2{border:0;margin:14px 0 6px;color:var(--ink)}.metodo ol{margin:0;padding-left:20px;font-size:15px}.metodo li{margin:6px 0}
.griglia{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.griglia a{display:flex;flex-direction:column;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;
  color:var(--ink);text-decoration:none;font:600 14px/1.3 system-ui,sans-serif}
.griglia a span{font-size:20px}.griglia small{font-weight:400;color:var(--muted);font-size:12px}
.griglia a.nuovi{border-color:var(--accent)}
.cat{margin-top:8px}.consiglio{font:14px system-ui,sans-serif;color:var(--muted);margin:0 0 6px}
.riga{position:relative;padding:12px 52px 12px 0;border-bottom:1px solid var(--line);background:none;border-radius:0;margin:0;border-left:0;border-right:0;border-top:0}
.riga h3{font-size:17px;margin:0 0 2px}.riga h3 a{color:var(--ink);text-decoration:none}
.riga p{margin:2px 0;font-size:15px}.riga .dove{margin:6px 0 0}.riga .stella{top:10px;right:0}
.riga .nota{font:13px system-ui,sans-serif;color:#b45309}
.costo{font:600 12px system-ui,sans-serif;border-radius:6px;padding:3px 8px}
.costo.gratis{background:#dcfce7;color:#15803d}.costo.limiti{background:#fef3c7;color:#92400e}.costo.pagamento{background:#fee2e2;color:#991b1b}
@media (prefers-color-scheme:dark){.costo.gratis{background:#14532d;color:#86efac}.costo.limiti{background:#451a03;color:#fcd34d}.costo.pagamento{background:#450a0a;color:#fca5a5}}
[hidden]{display:none!important}
.bollino{display:inline-block;min-width:18px;padding:1px 5px;margin-left:2px;border-radius:999px;background:var(--accent);
  color:#fff;font:700 11px/16px system-ui,sans-serif;text-align:center;vertical-align:1px}
.letti-bar{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:6px 0 10px;
  font:600 14px system-ui,sans-serif}
.letti-bar button,.daleggere button{font:600 13px system-ui,sans-serif;border:1px solid var(--line);background:var(--card);
  color:var(--ink);border-radius:999px;padding:6px 12px;cursor:pointer}
.arrivo.nonletto{border-left-color:var(--accent);box-shadow:0 0 0 2px var(--accent) inset}
.daleggere{display:flex;align-items:center;gap:10px;margin:0 44px 6px 0;font:700 12px system-ui,sans-serif;color:var(--accent)}
.daleggere button{padding:3px 10px;font-size:12px}
.cassetta-indice{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
.cassetta-indice a{font:600 13px system-ui,sans-serif;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 12px;text-decoration:none}
.osint .sub a{color:inherit}
.osint.nuovi{border-left-color:var(--accent)}.osint.nuovi h2{color:var(--accent)}
.osint .quando a{color:inherit}.osint details.altri{margin-top:10px;font:14px system-ui,sans-serif;color:var(--muted)}
.osint.mia h3 a{color:var(--ink);text-decoration:none}.osint.mia{border-left-color:#eab308}.osint.mia h2{color:#a16207}
@media (prefers-color-scheme:dark){.osint.mia h2{color:#facc15}}
.osint .quando{font:12px system-ui,sans-serif;color:var(--muted);margin:0 0 4px}.osint .vuoto{font:14px system-ui,sans-serif;color:var(--muted)}
.menu{display:flex;gap:4px;overflow-x:auto;margin:-12px -16px 18px;padding:10px 16px;border-bottom:1px solid var(--line);
  position:sticky;top:0;background:var(--bg);z-index:5;scrollbar-width:none}
.menu::-webkit-scrollbar{display:none}
.menu a{flex:none;font:600 14px system-ui,sans-serif;color:var(--muted);text-decoration:none;padding:7px 11px;border-radius:999px}
.menu a[aria-current]{color:var(--ink);background:var(--chip)}
.when a{color:inherit}.agg.vuota{border-style:dashed;border-width:1px}
.agg h2{margin-top:22px}.agg .quando,.try .quando{font:12px system-ui,sans-serif;color:var(--muted);margin:0 0 4px}
.brevi small{font:12px system-ui,sans-serif;color:var(--muted)}
.agg{border:2px solid var(--accent);border-radius:16px;padding:6px 14px 10px;margin:22px -4px 0}
.agg .tryzone{margin:12px -10px 0}
.agg-top{font:700 15px system-ui,sans-serif;color:var(--accent);margin:10px 0 2px}
.agg-top.mattino{margin-top:30px;padding-top:12px;border-top:1px solid var(--line)}
article{position:relative}article .tags{padding-right:40px}
.stella{position:absolute;top:10px;right:10px;width:38px;height:38px;border-radius:50%;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:20px;line-height:1;cursor:pointer}
.stella[aria-pressed="true"]{color:#eab308;border-color:#eab308;background:#fef9c3}
@media (prefers-color-scheme:dark){.stella[aria-pressed="true"]{background:#422006}}
.vaipref{font:600 14px system-ui,sans-serif;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:9px 14px;text-decoration:none}
.pref article h3 a{color:var(--ink)}.pref .vuoto{font:15px system-ui,sans-serif;color:var(--muted)}
.pref .quando{font:12px system-ui,sans-serif;color:var(--muted);margin:0 0 4px}
.togli{font:600 13px system-ui,sans-serif;color:var(--muted);background:none;border:1px solid var(--line);border-radius:999px;padding:5px 12px;cursor:pointer;margin-top:8px}
.filtri{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.filtri button{font:600 13px system-ui,sans-serif;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:6px 12px;cursor:pointer}
.filtri button[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
@media print{.osint{break-inside:auto}}
@media print{.menu{display:none!important}}
@media print{.listen,.week,.cerca,.risultati,.glossario,.prova,.stella,.vaipref,.togli,.filtri{display:none!important}}
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


ULTIMA_SETTIMANA = None   # (giorno, titolo) dell'ultima "Settimana dell'AI", impostata da rigenera_tutto()


def menu(prefisso, attivo=""):
    """Barra di navigazione comune a tutte le pagine (separata dai pulsanti di azione)."""
    voci = [("oggi", f"{prefisso}index.html", "📰 Oggi"),
            ("preferiti", f"{prefisso}preferiti.html", '⭐ Preferiti <span id="nPref"></span>'),
            ("osint", f"{prefisso}cassetta-osint.html", '🧰 OSINT <span id="nOsint" class="bollino" hidden></span>')]
    if ULTIMA_SETTIMANA:
        voci.append(("settimana", f"{prefisso}settimana/{ULTIMA_SETTIMANA[0]}.html", "📅 Settimana"))
    voci.append(("archivio", "#archivio", "🗂️ Archivio"))
    return ('<nav class="menu" aria-label="Sezioni del sito">'
            + "".join(f'<a href="{h}"{" aria-current=page" if k == attivo else ""}>{t}</a>' for k, h, t in voci)
            + "</nav>")


def stella(fid, titolo, testo, giorno, ancora, link="", tipo="", pagina=""):
    """Pulsante ☆ per salvare la scheda nei Preferiti (salvati nel browser di chi legge).
    `pagina` serve alle schede fuori dalle edizioni (es. "cassetta-osint.html#t3")."""
    return (f'<button type="button" class="stella" aria-pressed="false" title="Aggiungi ai preferiti" '
            f'aria-label="Aggiungi ai preferiti" data-id="{e(fid)}" data-titolo="{e(titolo)}" '
            f'data-testo="{e((testo or "")[:300])}" data-giorno="{e(giorno)}" data-ancora="{e(ancora)}" '
            f'data-link="{e(link)}" data-tipo="{e(tipo)}" data-pagina="{e(pagina)}" onclick="stella(this)">☆</button>')


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
    for d in r.get("da_provare", []) + r.get("osint", []):
        pezzi += [d.get("cosa", ""), d.get("a_cosa_serve", "")]
    for s in r.get("sezioni", []):
        for n in s.get("notizie", []):
            pezzi += [n.get("titolo", ""), n.get("riassunto", ""), n.get("perche_conta", "")]
    return " ".join(str(p) for p in pezzi)


def glossario_html(r, massimo=8):
    testo = " ".join(testo_della_giornata(x) for x in (r if isinstance(r, list) else [r]))
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
    return ('<h2 id="archivio">Archivio</h2>'
            '<input class="cerca" type="search" placeholder="🔎 Cerca nelle edizioni passate…" '
            'aria-label="Cerca nell\'archivio" oninput="cerca(this.value)" onfocus="caricaIndice()">'
            '<ul class="risultati" id="risultati" aria-live="polite"></ul>'
            f'<ul class="arch" id="arch">{voci}</ul>')


def legenda_html():
    righe = "".join(f"<dt>{badge(t) if t != 'Solo titoli' else badge('', True)}</dt><dd>{e(d)}</dd>"
                    for t, d in ETICHETTE)
    return f'<details><summary>Come leggere le etichette</summary><dl class="etichette">{righe}</dl></details>'


def ora_di(iso):
    try:
        return f"{datetime.fromisoformat(iso).astimezone(TZ):%H:%M}"
    except (TypeError, ValueError):
        return ""


def orari_italiani(giorno=None):
    """Gli orari di ORARI_UTC espressi nell'ora italiana del giorno indicato (cambia con l'ora legale)."""
    g = giorno or datetime.now(TZ)
    out = []
    for o in ORARI_UTC:
        h, m = map(int, o.split(":"))
        d = datetime(g.year, g.month, g.day, h, m, tzinfo=timezone.utc).astimezone(TZ)
        out.append(f"{d.hour}:{d.minute:02d}")
    return out


def scheda_strumento(d, per_id, giorno, ancora, tipo, nuovo_formato=True, etichette=None):
    """Una scheda strumento (Strumenti nuovi o OSINT) con stellina, piattaforme e pulsante Provalo."""
    if etichette is None:
        etichette = [d.get("per_chi"), d.get("ambito"), d.get("costo"), d.get("difficolta")]
        if str(d.get("italiano", "")).strip().lower() in ("sì", "si"):
            etichette.append("In italiano")
        if utile(d.get("disponibilita")):
            etichette.append(d["disponibilita"])
    tag = (badge(d.get("affidabilita", ""), d.get("solo_titoli") and nuovo_formato)
           + "".join(f'<span class="tag">{e(t)}</span>' for t in dict.fromkeys(etichette)
                     if utile(t) or t == "Costo non indicato"))
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
    dove = "".join(f'<span class="pf{" si" if t.startswith("✓") else " solo" if t.startswith("Solo") else ""}">'
                   f'{e(t)}</span>' for t in etichette_piattaforma(d))
    if d.get("sul_dispositivo"):
        dove += '<span class="pf locale">🔒 Funziona sul dispositivo</span>'
    dove = f'<div class="dove"><b>Dove si usa:</b> {dove}</div>' if dove else ""
    if d.get("come_sfruttarla"):
        come = f'<p class="why"><b>Come sfruttarla:</b> {e(d["come_sfruttarla"])}</p>' + come
    pref = stella(f"{giorno}#{ancora}", d.get("cosa", ""), d.get("a_cosa_serve", ""), giorno,
                  ancora, d.get("prova", ""), tipo)
    return (f'<article class="try" id="{ancora}">{pref}<div class="tags">{tag}</div>'
            f'<h3>{e(d.get("cosa"))}</h3>{dove}<p>{e(d.get("a_cosa_serve"))}</p>'
            f"{come}{prova}{chips(d['fonti'], per_id)}</article>")


def scheda_notizia(n, per_id, giorno, ancora, breve=False, ora=""):
    """Una notizia: riga breve (sezione "In breve") o scheda completa con stellina."""
    if breve:
        return (f'<li id="{ancora}">{f"<small>🕐 {e(ora)}</small> " if ora else ""}<b>{e(n["titolo"])}.</b> '
                f"{e(n.get('riassunto'))} {chips(n['fonti'][:2], per_id)}</li>")
    perche = (f'<p class="why"><b>Perché conta:</b> {e(n["perche_conta"])}</p>'
              if n.get("perche_conta") else "")
    if n.get("come_provarla"):
        perche += f'<p class="why"><b>Come provarla:</b> {e(n["come_provarla"])}</p>'
    extra = "".join(f'<span class="tag">{e(t)}</span>'
                    for t in (n.get("strumento"), n.get("disponibilita")) if utile(t))
    pref = stella(f"{giorno}#{ancora}", n.get("titolo", ""), n.get("riassunto", ""), giorno,
                  ancora, "", n.get("strumento") or "Novità")
    quando = f'<p class="quando">🕐 ore {e(ora)}</p>' if ora else ""
    return (f'<article id="{ancora}">{pref}{quando}<div class="tags">{badge(n.get("affidabilita", ""), n.get("solo_titoli"))}'
            f"{extra}</div><h3>{e(n['titolo'])}</h3><p>{e(n.get('riassunto'))}</p>"
            f"{perche}{chips(n['fonti'], per_id)}</article>")


def blocco_aggiornamenti(aggiornamenti, per_id, giorno, prefisso, ora_mattino):
    """Un solo riquadro con TUTTI gli aggiornamenti della giornata, divisi per tipo, ogni scheda con la sua ora."""
    if not aggiornamenti:
        return []
    strumenti, osint, novita, brevi = [], [], [], []
    for i in range(len(aggiornamenti), 0, -1):           # dal più recente
        u = aggiornamenti[i - 1]
        ru, ora = u.get("riassunto") or {}, ora_di(u.get("generato"))

        def con_ora(h, ora=ora):
            return h.replace('<div class="tags">', f'<p class="quando">🕐 ore {e(ora)}</p><div class="tags">', 1)
        for j, d in enumerate(ru.get("da_provare", []), 1):
            strumenti.append(con_ora(scheda_strumento(d, per_id, giorno, f"u{i}p{j}", "Strumento", True)))
        for j, d in enumerate(ru.get("osint", []) or [], 1):
            osint.append(con_ora(scheda_strumento(d, per_id, giorno, f"u{i}o{j}", "OSINT", True, etichette=[
                d.get("uso"), d.get("costo") or "Costo non indicato", d.get("difficolta")])))
        k = 0
        for s in ru.get("sezioni", []):
            for n in s.get("notizie", []):
                k += 1
                if s.get("breve"):
                    brevi.append(scheda_notizia(n, per_id, giorno, f"u{i}n{k}", True, ora))
                else:
                    novita.append(scheda_notizia(n, per_id, giorno, f"u{i}n{k}", False, ora))
    ultima = ora_di(aggiornamenti[-1].get("generato"))
    sotto = (f"Solo ciò che è uscito dopo l'edizione del mattino{f' ({e(ora_mattino)})' if ora_mattino else ''}. "
             f"Ultimo controllo alle {e(ultima)}.")
    if not (strumenti or osint or novita or brevi):
        return [f'<section class="agg vuota" id="aggiornamenti"><p class="agg-top">🆕 Aggiornamenti di oggi</p>'
                f'<p class="sub">{sotto} Nessuna novità rispetto a stamattina.</p></section>']
    parti = [f'<section class="agg" id="aggiornamenti"><p class="agg-top">🆕 Aggiornamenti di oggi</p><p class="sub">{sotto}</p>']
    if strumenti:
        parti += ['<h2>🧪 Strumenti nuovi</h2>'] + strumenti
    if osint:
        parti += ['<h2>🔎 Strumenti OSINT</h2>'] + osint
    if novita:
        parti += [f'<h2>{e(SEZ_NOVITA)}</h2>'] + novita
    if brevi:
        parti += [f'<h2>{e(SEZ_BREVE)}</h2><ul class="brevi">'] + brevi + ["</ul>"]
    parti.append("</section>")
    return parti


def contenuto(r, per_id, giorno, pre="", prefisso=""):
    """Strumenti, novità e "In breve" di un'edizione. `pre` distingue gli aggiornamenti (es. "u1")."""
    parti = []
    nuovo_formato = r.get("formato") == 2
    nome_zona = "🧪 Strumenti nuovi" if nuovo_formato else "🧪 Da provare oggi"
    if not pre and not r.get("da_provare") and r.get("sezioni") and "non disponibile" not in str(r.get("in_breve")):
        parti.append(f'<section class="tryzone"><h2>{nome_zona}</h2><p class="sub">Oggi nessuno strumento nuovo '
                     'abbastanza concreto da consigliare: meglio niente che un suggerimento debole.</p></section>')
    if r.get("da_provare"):
        sotto = ("App e servizi AI appena usciti, da usare al lavoro o tutti i giorni" if nuovo_formato
                 else "Nuovi strumenti e possibilità emersi dalle notizie di oggi")
        parti.append(f'<section class="tryzone"><h2>{nome_zona}</h2><p class="sub">{sotto}</p>')
        for j, d in enumerate(r["da_provare"], 1):
            parti.append(scheda_strumento(d, per_id, giorno, f"{pre}p{j}", "Strumento", nuovo_formato))
        parti.append("</section>")
    if r.get("osint"):
        parti.append('<section class="osint"><h2>🔎 Strumenti OSINT</h2><p class="sub">Strumenti per indagini su '
                     'fonti aperte (verifica, geolocalizzazione, aziende, siti, persone e account) usabili nel browser '
                     'o su Windows. Usali solo per scopi leciti: sui dati delle persone vale il GDPR. '
                     f'Gli strumenti consolidati sono nella <a href="{prefisso}cassetta-osint.html" '
                     'class="link-cassetta">🧰 Cassetta degli attrezzi OSINT</a>.</p>')
        for j, d in enumerate(r["osint"], 1):
            parti.append(scheda_strumento(d, per_id, giorno, f"{pre}o{j}", "OSINT", True,
                                          etichette=[d.get("uso"), d.get("costo") or "Costo non indicato",
                                                     d.get("difficolta")]))
        parti.append("</section>")
    k = 0
    for s in r.get("sezioni", []):
        parti.append(f"<h2>{e(s['titolo'])}</h2>")
        breve = s.get("breve")
        if breve:
            parti.append('<ul class="brevi">')
        for n in s["notizie"]:
            k += 1
            parti.append(scheda_notizia(n, per_id, giorno, f"{pre}n{k}", breve))
        if breve:
            parti.append("</ul>")
    return parti


def pagina(dati, archivio, prefisso, settimana=None):
    r = dati["riassunto"]
    per_id = dati.get("articoli", {})
    stato = dati.get("fonti", [])
    modello = dati.get("modello", "")
    giorno = datetime.strptime(dati["giorno"], "%Y-%m-%d")
    data_it = data_italiana(giorno)
    aggiornamenti = dati.get("aggiornamenti", [])
    ora_mattino = ora_di(dati.get("generato"))
    ultima_ora = ora_di(aggiornamenti[-1].get("generato")) if aggiornamenti else ora_mattino
    orari = " e le ".join(orari_italiani(giorno))
    quando = ""
    if ora_mattino:
        quando = f"Edizione delle {ora_mattino}"
        if aggiornamenti and ultima_ora != ora_mattino:
            quando += f' · <a href="#aggiornamenti">aggiornata alle {ultima_ora}</a>'
    if prefisso == "":
        # Solo nella pagina di oggi: il prossimo aggiornamento (calcolato dal browser, vedi prossimo() nel JS)
        quando += (f' · <span id="prossimo">aggiornamenti ogni giorno verso le {e(orari)}</span>')
    parti = [menu(prefisso, "oggi" if prefisso == "" else "archivio"),
             f'<p class="top">AI News Digest · {e(data_it)}</p>',
             f"<h1>{e(r.get('titolo_giorno'))}</h1>",
             f'<p class="when">🕖 {quando}</p>' if quando else "",
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
    # Tutti gli aggiornamenti della giornata in un solo riquadro, sopra l'edizione del mattino.
    blocco = blocco_aggiornamenti(aggiornamenti, per_id, dati["giorno"], prefisso, ora_mattino)
    parti += blocco
    if blocco:
        parti.append(f'<p class="agg-top mattino">☀️ Edizione del mattino{f" · ore {e(ora_mattino)}" if ora_mattino else ""}</p>')
    parti += contenuto(r, per_id, dati["giorno"], "", prefisso)
    parti.append(glossario_html([r] + [u.get("riassunto") or {} for u in aggiornamenti]))
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
    dati_js = json.dumps({"titolo": titolo_pag, "testo": testo_whatsapp(data_it, r, url_pagina, aggiornamenti),
                          "url": url_pagina, "orari": ORARI_UTC}, ensure_ascii=False).replace("</", "<\\/")
    return (testa(f"{titolo_pag} — {r.get('titolo_giorno', '')}", r.get("in_breve", ""), url_pagina, prefisso)
            + f"<body><main>{''.join(parti)}</main>" + piede(prefisso, dati_js))


def pagina_settimana(w, archivio):
    prefisso = "../"
    fine = datetime.strptime(w["giorno"], "%Y-%m-%d")
    inizio = fine - timedelta(days=6)
    periodo = f"dal {inizio.day} {MESI_IT[inizio.month - 1]} al {fine.day} {MESI_IT[fine.month - 1]} {fine.year}"
    parti = [menu(prefisso, "settimana"),
             f'<p class="top">La settimana dell\'AI · {e(periodo)}</p>',
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
        link = "".join(f'<a href="{prefisso}giorni/{g}.html#{f"n{k}" if isinstance(k, int) else e(k)}">'
                       f'{e(data_breve(g))} ↗</a>' for g, k in n.get("rif", []))
        perche = (f'<p class="why"><b>Perché conta:</b> {e(n.get("perche_conta"))}</p>'
                  if n.get("perche_conta") else "")
        blocco_notizie.append(f"<article><h3>{i}. {e(n.get('titolo'))}</h3><p>{e(n.get('racconto'))}</p>"
                              f'{perche}<div class="src">{link}</div></article>')
    parti += (blocco_prove + blocco_notizie) if nuovo else (blocco_notizie + blocco_prove)
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


PAGINA_PREFERITI_JS = """
let filtro='tutti';
function disegna(){
  const tutti=leggiPref(),box=document.getElementById('lista');box.innerHTML='';
  const gruppo=p=>p.tipo==='Strumento'?'Strumenti':p.tipo==='OSINT'?'OSINT':'Novità';
  const tipi=['tutti',...new Set(tutti.map(gruppo))];
  const f=document.getElementById('filtri');f.innerHTML='';
  if(tutti.length>1&&tipi.length>2){for(const t of tipi){const b=document.createElement('button');b.type='button';
    b.textContent=t==='tutti'?'Tutti':t;b.setAttribute('aria-pressed',filtro===t);b.onclick=()=>{filtro=t;disegna();};f.append(b);}}
  const l=tutti.filter(p=>filtro==='tutti'||gruppo(p)===filtro);
  document.getElementById('conta').textContent=tutti.length?(tutti.length===1?'1 scheda salvata':tutti.length+' schede salvate'):'';
  if(!l.length){box.innerHTML='<p class="vuoto">Nessun preferito per ora. Tocca la stellina ☆ su uno strumento o una novità e lo ritrovi qui.</p>';return;}
  for(const p of l){
    const a=document.createElement('article'),q=document.createElement('p'),h=document.createElement('h3'),
      t=document.createElement('a'),x=document.createElement('p'),src=document.createElement('div'),togli=document.createElement('button');
    q.className='quando';q.textContent=quandoPref(p);
    t.href=schedaPref(p);t.textContent=p.titolo;h.append(t);x.textContent=p.testo||'';
    src.className='src';
    if(p.link){const pr=document.createElement('a');pr.className='prova';pr.href=p.link;pr.target='_blank';pr.rel='noopener';pr.textContent='Provalo ↗';a.append(q,h,x,pr);}
    else{a.append(q,h,x);}
    const sc=document.createElement('a');sc.href=t.href;sc.textContent='Apri la scheda ↗';src.append(sc);
    togli.className='togli';togli.type='button';togli.textContent='✕ Togli';
    togli.onclick=()=>{scriviPref(leggiPref().filter(y=>y.id!==p.id));disegna();};
    a.append(src,togli);box.append(a);
  }
}
function copiaPreferiti(b){
  const l=leggiPref();if(!l.length){alertino('Nessun preferito da copiare');return;}
  const base=/^http/.test(DIGEST.url||'')?DIGEST.url.replace(/preferiti\\.html$/,''):location.href.replace(/preferiti\\.html.*$/,'');
  const t='⭐ *I miei strumenti AI preferiti*\\n\\n'+l.map(p=>'• *'+p.titolo+'*'+(p.testo?': '+p.testo:'')+'\\n  '+(p.link||base+schedaPref(p))).join('\\n\\n');
  copia(t,b,'✓ Copiato!');
}
document.addEventListener('DOMContentLoaded',disegna);
addEventListener('pageshow',disegna);
"""


def pagina_preferiti(archivio):
    url_pagina = indirizzo_sito() + "preferiti.html"
    parti = [menu("", "preferiti"), '<p class="top">AI News Digest</p>', "<h1>⭐ I miei preferiti</h1>",
             '<p class="lead">Gli strumenti e le novità che hai segnato con la stellina, per ritrovarli quando ti servono.</p>',
             '<p class="meta" id="conta"></p>',
             '<div class="share"><button type="button" onclick="copiaPreferiti(this)">💬 Copia elenco per WhatsApp</button></div>',
             '<div class="filtri" id="filtri"></div><section class="pref" id="lista"></section>',
             '<p class="meta">I preferiti restano salvati in questo browser, su questo dispositivo. '
             'Se cancelli i dati di navigazione o usi la navigazione privata non vengono conservati.</p>']
    parti.append(archivio_html(archivio, ""))
    dati_js = json.dumps({"titolo": "I miei preferiti", "testo": "", "url": url_pagina},
                         ensure_ascii=False).replace("</", "<\\/")
    return (testa("AI News Digest — I miei preferiti", "Gli strumenti AI che ho salvato", url_pagina, "")
            + f"<body><main>{''.join(parti)}</main>"
            + piede("", dati_js).replace("</script>", PAGINA_PREFERITI_JS + "</script>", 1))


def nuovi_arrivi(edizioni, cassetta):
    """Tutti gli strumenti OSINT segnalati nelle edizioni (anche negli aggiornamenti), dal più recente,
    senza doppioni e senza quelli già presenti tra i consolidati della cassetta."""
    norm = lambda t: re.sub(r"\W+", "", (t or "").lower())
    consolidati = {norm(t["nome"]) for c in cassetta.get("categorie", []) for t in c.get("strumenti", [])}
    visti, out = set(), []
    for x in sorted(edizioni, key=lambda x: x["giorno"], reverse=True):
        per_id = x.get("articoli", {})
        for pre, r in reversed(list(riassunti_del_giorno(x))):
            for j, d in enumerate(r.get("osint", []) or [], 1):
                chiave = norm(d.get("cosa"))
                if not chiave or chiave in visti or chiave in consolidati:
                    continue
                visti.add(chiave)
                out.append((x["giorno"], f"{pre}o{j}", d, per_id))
    return out


METODO_OSINT = [
    ("1", "La domanda", "Scrivi in una riga cosa vuoi verificare: «questa foto è di oggi?», «questa ditta esiste?»."),
    ("2", "Le fonti", "Parti dalle fonti ufficiali e primarie (registri, siti delle aziende, l'originale di una foto)."),
    ("3", "Prima la raccolta passiva", "Archivi, cache, registri e motori di ricerca non avvisano nessuno. Gli strumenti "
     "che contattano il bersaglio (scansioni, verifiche attive) vengono dopo, solo se servono e se è lecito."),
    ("4", "L'incrocio", "Nessuno strumento basta da solo: conferma ogni dato con almeno due strumenti diversi."),
    ("5", "Le prove", "Salva pagine e risultati con data e ora (archive.today, Hunchly): domani potrebbero sparire."),
]


def riga_strumento(t, prefisso=""):
    """Riga compatta per uno strumento consolidato della cassetta."""
    ancora = "t" + re.sub(r"\W+", "-", t["nome"].lower()).strip("-")
    pref = stella(f"cassetta#{ancora}", t["nome"], t.get("descrizione", ""), "", ancora, t["url"], "OSINT",
                  f"cassetta-osint.html#{ancora}")
    dove = "".join(f'<span class="pf{" si" if x.startswith("✓") else " solo" if x.startswith("Solo") else ""}">'
                   f'{e(x)}</span>' for x in etichette_piattaforma(t))
    costo = t.get("costo", "")
    classe_costo = "gratis" if costo == "Gratis" else "pagamento" if costo == "A pagamento" else "limiti"
    info = f'<span class="costo {classe_costo}">{e(costo)}</span>' if costo else ""
    if t.get("difficolta") and t["difficolta"] != "Facile":
        info += f'<span class="pf">{e(t["difficolta"])}</span>'
    nota = f'<p class="nota">⚠️ {e(t["nota"])}</p>' if t.get("nota") else ""
    return (f'<article class="riga" id="{ancora}">{pref}'
            f'<h3><a href="{e(t["url"])}" target="_blank" rel="noopener">{e(t["nome"])} ↗</a></h3>'
            f'<p>{e(t.get("descrizione"))}</p>{nota}<div class="dove">{dove}{info}</div></article>')


def pagina_cassetta(cassetta, archivio, edizioni=()):
    """Pagina OSINT: metodo, indice per tipo di indagine, i tuoi strumenti, i nuovi arrivi (automatici)
    e gli strumenti consolidati (fissi, da cassetta_osint.json) in righe compatte."""
    url_pagina = indirizzo_sito() + "cassetta-osint.html"
    cat = cassetta.get("categorie", [])
    arrivi = nuovi_arrivi(edizioni, cassetta)
    totale = sum(len(c.get("strumenti", [])) for c in cat)
    aggiornato = cassetta.get("aggiornato", "")
    parti = [menu("", "osint"),
             '<p class="top">AI News Digest · OSINT</p>', "<h1>🧰 Cassetta degli attrezzi OSINT</h1>",
             f'<p class="lead">{totale} strumenti collaudati per le indagini su fonti aperte, divisi per tipo di '
             'indagine. Sotto si aggiungono da soli i nuovi strumenti segnalati ogni giorno.</p>',
             '<section class="metodo"><h2>Come si imposta un\'indagine</h2><ol>'
             + "".join(f"<li><b>{e(t)}.</b> {e(d)}</li>" for _, t, d in METODO_OSINT) + "</ol></section>",
             '<h2>Che cosa devi verificare?</h2><nav class="griglia">'
             + "".join(f'<a href="#c{i}"><span>{e(c.get("icona", ""))}</span>{e(c["titolo"])}'
                       f'<small>{len(c.get("strumenti", []))} strument{"o" if len(c.get("strumenti", [])) == 1 else "i"}</small></a>' for i, c in enumerate(cat, 1))
             + (f'<a href="#nuovi" class="nuovi"><span>🆕</span>Nuovi arrivi<small>{len(arrivi)} strument{"o" if len(arrivi) == 1 else "i"}</small></a>'
                if arrivi else "") + "</nav>",
             # si mostra solo se ci sono strumenti salvati (vedi disegnaMieiOsint nel JS)
             '<section class="osint mia" id="mia" hidden><h2>⭐ I tuoi strumenti OSINT</h2>'
             '<p class="sub">Quelli che hai segnato con la stellina, qui e nelle edizioni. Salvati su questo dispositivo.</p>'
             '<div id="mieiOsint"></div></section>']
    if arrivi:
        # Nuovi arrivi: in alto quelli ancora da leggere; quelli già letti finiscono in "Già letti" (vedi JS).
        parti.append('<section class="osint nuovi" id="nuovi"><h2>🆕 Nuovi arrivi</h2><p class="sub">Strumenti OSINT '
                     'segnalati nelle edizioni del giorno, dal più recente. Si aggiungono da soli e restano. Sono '
                     'novità: provali con prudenza, non sono ancora collaudati come quelli più sotto.</p>'
                     '<div class="letti-bar"><span id="nonLetti"></span>'
                     '<button type="button" id="tuttiLetti" onclick="segnaTuttiLetti()">✓ Segna tutti come letti</button></div>'
                     '<div id="arriviNuovi">')
        for giorno, ancora, d, per_id in arrivi:
            lid = f"{giorno}#{ancora}"
            parti.append(scheda_strumento(d, per_id, giorno, ancora, "OSINT", True,
                                          etichette=[d.get("uso"), d.get("costo") or "Costo non indicato",
                                                     d.get("difficolta")])
                         .replace('<div class="tags">', f'<p class="quando">Segnalato il {e(data_breve(giorno))} '
                                  f'{giorno[:4]} · <a href="giorni/{giorno}.html#{ancora}">vedi l\'edizione</a></p>'
                                  '<div class="tags">', 1)
                         .replace(f'<article class="try" id="{ancora}"',
                                  f'<article class="try arrivo" data-lid="{e(lid)}" id="n-{giorno}-{ancora}"', 1))
        parti.append('</div><details class="altri" id="arriviLetti" hidden><summary>Già letti</summary></details>'
                     "</section>")
    for i, c in enumerate(cat, 1):
        parti.append(f'<section class="cat" id="c{i}"><h2>{e(c.get("icona", ""))} {e(c["titolo"])}</h2>')
        if c.get("consiglio"):
            parti.append(f'<p class="consiglio">💡 {e(c["consiglio"])}</p>')
        parti += [riga_strumento(t) for t in c.get("strumenti", [])]
        parti.append("</section>")
    parti.append('<p class="meta">Usa questi strumenti solo per scopi leciti: sui dati delle persone vale il GDPR. '
                 'Costi e condizioni cambiano, controlla sul sito di ciascuno'
                 + (f" (elenco rivisto il {e(data_breve(aggiornato))} {e(aggiornato[:4])})" if aggiornato else "") + ".</p>")
    parti.append(archivio_html(archivio, ""))
    dati_js = json.dumps({"titolo": "Cassetta degli attrezzi OSINT", "testo": "Cassetta degli attrezzi OSINT: "
                          + url_pagina, "url": url_pagina}, ensure_ascii=False).replace("</", "<\\/")
    return (testa("AI News Digest — Cassetta degli attrezzi OSINT",
                  "Strumenti OSINT collaudati, divisi per tipo di indagine, con piattaforma e costo.", url_pagina, "")
            + f"<body><main>{''.join(parti)}</main>" + piede("", dati_js))


def indirizzo_sito():
    """Indirizzo GitHub Pages, ricavato dal nome del repository (disponibile dentro GitHub Actions)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return os.environ.get("SITO_URL", "")
    utente, nome = repo.split("/", 1)
    if nome.lower() == f"{utente.lower()}.github.io":
        return f"https://{utente.lower()}.github.io/"
    return f"https://{utente.lower()}.github.io/{nome}/"


def testo_whatsapp(data_it, r, url, aggiornamenti=()):
    """Versione testuale breve, con la formattazione di WhatsApp (*grassetto*, _corsivo_)."""
    righe = [f"🤖 *AI News Digest* · {data_it}", "", f"*{r.get('titolo_giorno', '')}*",
             r.get("in_breve", ""), ""]
    for u in reversed(list(aggiornamenti)):
        ru = u.get("riassunto") or {}
        voci = [f"• *{d.get('cosa', '')}*: {d.get('a_cosa_serve', '')}"
                for d in ru.get("da_provare", []) + ru.get("osint", [])]
        voci += [f"• {n.get('titolo', '')}" for x in ru.get("sezioni", []) for n in x.get("notizie", [])]
        if voci:
            righe += [f"🆕 *Aggiornamento delle {ora_di(u.get('generato'))}*"] + voci + [""]
    nuovo = r.get("formato") == 2
    if r.get("da_provare"):
        righe.append("🧪 *Strumenti nuovi*" if nuovo else "🧪 *Da provare oggi*")
        for d in r["da_provare"]:
            info = [d["costo"]] if nuovo and utile(d.get("costo")) else []
            info += [t.replace("✓ ", "") for t in etichette_piattaforma(d)] if nuovo else []
            info += ["sul dispositivo"] if d.get("sul_dispositivo") else []
            extra = f" ({', '.join(info)})" if info else ""
            righe.append(f"• *{d.get('cosa', '')}*{extra}: {d.get('a_cosa_serve', '')}")
        righe.append("")
    if r.get("osint"):
        righe.append("🔎 *Strumenti OSINT*")
        for d in r["osint"]:
            info = [d.get("costo") or "Costo non indicato"] + [t.replace("✓ ", "") for t in etichette_piattaforma(d)]
            righe.append(f"• *{d.get('cosa', '')}* ({', '.join(info)}): {d.get('a_cosa_serve', '')}")
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
      a.href=PREFISSO+'giorni/'+g+'.html'+(n[0]?'#'+(typeof n[0]==='number'?'n'+n[0]:n[0]):'');
      const s=document.createElement('small');s.textContent=g.split('-').reverse().join('/');
      a.append(s,document.createTextNode(n[1]));li.append(a);ul.append(li);
    }
  });
}
const CHIAVE_PREF='ai-news-preferiti';
function leggiPref(){try{return JSON.parse(localStorage.getItem(CHIAVE_PREF)||'[]')||[];}catch(e){return [];}}
function scriviPref(l){try{localStorage.setItem(CHIAVE_PREF,JSON.stringify(l));return true;}catch(e){alertino('Il browser non permette di salvare i preferiti');return false;}}
function schedaPref(p,pre){return (pre||'')+(p.pagina||('giorni/'+p.giorno+'.html#'+p.ancora));}
function quandoPref(p){return (p.tipo||'')+' · '+(p.giorno?p.giorno.split('-').reverse().join('/'):'Cassetta OSINT');}
function disegnaMieiOsint(){
  const box=document.getElementById('mieiOsint');if(!box)return;
  const l=leggiPref().filter(p=>p.tipo==='OSINT');box.innerHTML='';
  const sez=document.getElementById('mia');if(sez)sez.hidden=!l.length;
  if(!l.length)return;
  for(const p of l){
    const a=document.createElement('article'),h=document.createElement('h3'),t=document.createElement('a'),x=document.createElement('p'),q=document.createElement('p');
    a.className='try';q.className='quando';q.textContent=quandoPref(p);
    t.href=schedaPref(p,PREFISSO);t.textContent=p.titolo;h.append(t);x.textContent=p.testo||'';a.append(q,h,x);
    if(p.link){const pr=document.createElement('a');pr.className='prova';pr.href=p.link;pr.target='_blank';pr.rel='noopener';pr.textContent='Apri ↗';a.append(pr);}
    box.append(a);
  }
}
function aggiornaStelle(){
  const ids=new Set(leggiPref().map(p=>p.id));
  document.querySelectorAll('.stella').forEach(b=>{const on=ids.has(b.dataset.id);b.setAttribute('aria-pressed',on);
    b.textContent=on?'★':'☆';b.title=on?'Togli dai preferiti':'Aggiungi ai preferiti';b.setAttribute('aria-label',b.title);});
  const n=document.getElementById('nPref');if(n)n.textContent=ids.size?'('+ids.size+')':'';
  disegnaMieiOsint();
}
function stella(b){
  let l=leggiPref();const d=b.dataset;
  if(l.some(p=>p.id===d.id)){l=l.filter(p=>p.id!==d.id);if(scriviPref(l))alertino('Tolto dai preferiti');}
  else{l.unshift({id:d.id,titolo:d.titolo,testo:d.testo,giorno:d.giorno,ancora:d.ancora,link:d.link,tipo:d.tipo,pagina:d.pagina||'',
    salvato:new Date().toISOString().slice(0,10)});if(scriviPref(l))alertino('⭐ Aggiunto ai preferiti');}
  aggiornaStelle();
}
document.addEventListener('DOMContentLoaded',aggiornaStelle);
function prossimo(){
  const el=document.getElementById('prossimo');if(!el||!DIGEST.orari||!DIGEST.orari.length)return;
  const ora=new Date();let p=null;
  for(let g=0;g<3;g++)for(const o of DIGEST.orari){const [h,m]=o.split(':').map(Number);
    const d=new Date(Date.UTC(ora.getUTCFullYear(),ora.getUTCMonth(),ora.getUTCDate()+g,h,m));if(d>ora&&(!p||d<p))p=d;}
  if(!p)return;
  const roma={timeZone:'Europe/Rome'},giorno=x=>x.toLocaleDateString('it-IT',roma);
  const domani=new Date(ora.getTime()+864e5);
  const quale=giorno(p)===giorno(ora)?'oggi':giorno(p)===giorno(domani)?'domani':giorno(p);
  el.textContent='prossimo aggiornamento '+quale+' verso le '+p.toLocaleTimeString('it-IT',{...roma,hour:'2-digit',minute:'2-digit'});
}
document.addEventListener('DOMContentLoaded',prossimo);
const CHIAVE_LETTI='ai-news-osint-letti';
function leggiLetti(){try{return new Set(JSON.parse(localStorage.getItem(CHIAVE_LETTI)||'[]'));}catch(e){return new Set();}}
function scriviLetti(st){try{localStorage.setItem(CHIAVE_LETTI,JSON.stringify([...st]));}catch(e){}}
function segnaLetto(id){const st=leggiLetti();st.add(id);scriviLetti(st);aggiornaLetti();}
function segnaTuttiLetti(){const st=leggiLetti();document.querySelectorAll('.arrivo').forEach(a=>st.add(a.dataset.lid));
  scriviLetti(st);aggiornaLetti();alertino('✓ Tutti segnati come letti');}
function aggiornaLetti(){
  const letti=leggiLetti(),box=document.getElementById('arriviNuovi'),vecchi=document.getElementById('arriviLetti');
  if(box&&vecchi){
    let nuovi=0,giaLetti=0;
    document.querySelectorAll('.arrivo').forEach(a=>{
      const letto=letti.has(a.dataset.lid);a.classList.toggle('nonletto',!letto);
      let b=a.querySelector('.daleggere');
      if(!letto&&!b){b=document.createElement('div');b.className='daleggere';
        b.innerHTML='<span>● Da leggere</span><button type="button">✓ Letto</button>';
        b.querySelector('button').onclick=()=>segnaLetto(a.dataset.lid);a.prepend(b);
        a.querySelectorAll('a').forEach(l=>l.addEventListener('click',()=>segnaLetto(a.dataset.lid)));}
      if(letto&&b)b.remove();
      if(letto){vecchi.append(a);giaLetti++;}else{box.append(a);nuovi++;}
    });
    vecchi.hidden=!giaLetti;vecchi.querySelector('summary').textContent='Già letti ('+giaLetti+')';
    const t=document.getElementById('nonLetti'),bt=document.getElementById('tuttiLetti');
    if(t)t.textContent=nuovi?(nuovi===1?'1 da leggere':nuovi+' da leggere'):'Nessuna novità da leggere ✓';
    if(bt)bt.hidden=!nuovi;
    const tile=document.querySelector('.griglia a.nuovi small');if(tile)tile.textContent=nuovi?nuovi+' da leggere':'tutto letto';
    mostraBollino(nuovi);return;
  }
  fetch(PREFISSO+'osint-arrivi.json',{cache:'no-cache'}).then(r=>r.ok?r.json():[]).then(ids=>{
    mostraBollino(ids.filter(id=>!letti.has(id)).length);}).catch(()=>{});
}
function mostraBollino(n){const b=document.getElementById('nOsint');if(b){b.hidden=!n;b.textContent=n;}}
document.addEventListener('DOMContentLoaded',aggiornaLetti);
addEventListener('pageshow',aggiornaStelle);
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


def riassunti_del_giorno(x):
    """L'edizione del mattino ("" ) e gli aggiornamenti ("u1", "u2"…), con il prefisso delle loro ancore."""
    yield "", x.get("riassunto") or {}
    for i, u in enumerate(x.get("aggiornamenti", []), 1):
        if u.get("riassunto"):
            yield f"u{i}", u["riassunto"]


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
        voci = []
        for pre, r in riassunti_del_giorno(x):
            k = 0
            for s in r.get("sezioni", []):
                for n in s.get("notizie", []):
                    k += 1
                    voci.append([k if not pre else f"{pre}n{k}", n.get("titolo", ""), (n.get("riassunto") or "")[:220]])
            for j, d in enumerate(r.get("da_provare", []), 1):
                voci.append([f"{pre}p{j}", "🧪 " + d.get("cosa", ""), (d.get("a_cosa_serve") or "")[:220]])
            for j, d in enumerate(r.get("osint", []), 1):
                voci.append([f"{pre}o{j}", "🔎 " + d.get("cosa", ""), (d.get("a_cosa_serve") or "")[:220]])
        indice.append({"g": x["giorno"], "t": x["riassunto"].get("titolo_giorno", ""), "n": voci})
    (DOCS / "indice.json").write_text(json.dumps(indice, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")

    settimane = [x for x in (leggi_json(p) for p in sorted(SETTIMANE.glob("*.json"))) if x]
    global ULTIMA_SETTIMANA
    ULTIMA_SETTIMANA = (settimane[-1]["giorno"], settimane[-1].get("titolo", "")) if settimane else None
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
    (DOCS / "preferiti.html").write_text(pagina_preferiti(archivio), encoding="utf-8")
    cassetta = leggi_json(ROOT / "cassetta_osint.json") if (ROOT / "cassetta_osint.json").exists() else None
    if cassetta:
        (DOCS / "cassetta-osint.html").write_text(pagina_cassetta(cassetta, archivio, edizioni), encoding="utf-8")
        (DOCS / "osint-arrivi.json").write_text(json.dumps(
            [f"{g}#{a}" for g, a, _, _ in nuovi_arrivi(edizioni, cassetta)]), encoding="utf-8")
    print(f"Pagine aggiornate: {len(edizioni)} edizioni, {len(settimane)} settimanali.")


# ---------------------------------------------------------------- la settimana dell'AI (domenica)

ISTRUZIONI_SETTIMANA = """Sei il redattore di "AI News Digest", una guida in ITALIANO agli strumenti di intelligenza
artificiale per persone NON programmatrici, da usare al lavoro e nella vita di tutti i giorni.
Ricevi quanto pubblicato negli ultimi 7 giorni: strumenti nuovi (codice [AAAA-MM-GG#provare-n]) e novità
o notizie (codice [AAAA-MM-GG#n]).
1. Scegli i 5 MIGLIORI strumenti nuovi della settimana (o meno, se non sono validi): preferisci quelli
   gratuiti o con prova gratuita, utili a molte persone, disponibili in Italia, usabili nel browser o su
   Android (il lettore usa Android e Windows). Dai risalto alle AI che funzionano sul dispositivo.
   Puoi includere 1 strumento OSINT (codice [AAAA-MM-GG#osint-n]) se è davvero notevole.
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
        for pre, r in riassunti_del_giorno(x):
            k = 0
            for s in r.get("sezioni", []):
                for n in s.get("notizie", []):
                    k += 1
                    cod = f"{x['giorno']}#{pre}{k}" if not pre else f"{x['giorno']}#{pre}n{k}"
                    validi.add(cod)
                    righe.append(f"[{cod}] ({n.get('affidabilita', '')}) {n.get('titolo', '')} — "
                                 f"{n.get('riassunto', '')}")
            for j, d in enumerate(r.get("osint", []), 1):
                cod = f"{x['giorno']}#{pre}osint-{j}"
                prove[cod] = x["giorno"]
                info = [d.get("costo") or "Costo non indicato"] + piattaforme_di(d)
                righe.append(f"[{cod}] STRUMENTO OSINT: {d.get('cosa', '')} ({', '.join(info)}) — "
                             f"{d.get('a_cosa_serve', '')}")
            for j, d in enumerate(r.get("da_provare", []), 1):
                cod = f"{x['giorno']}#{pre}provare-{j}"
                prove[cod] = x["giorno"]
                info = ([d["costo"]] if utile(d.get("costo")) else []) + piattaforme_di(d)
                info += ["sul dispositivo"] if d.get("sul_dispositivo") else []
                costo = f" ({', '.join(info)})" if info else ""
                righe.append(f"[{cod}] STRUMENTO: {d.get('cosa', '')}{costo} — {d.get('a_cosa_serve', '')}")
    w, modello = chiama_gemini(ISTRUZIONI_SETTIMANA, "NOTIZIE DELLA SETTIMANA:\n" + "\n".join(righe), chiave)
    notizie = []
    for n in w.get("notizie", []):
        rif = [c for c in dict.fromkeys(n.get("rif", [])) if c in validi]
        if rif and n.get("titolo"):
            n["rif"] = [(c.split("#")[0], int(c.split("#")[1]) if c.split("#")[1].isdigit() else c.split("#")[1])
                        for c in rif]
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

def controlla_salute_fonti(stato_oggi, giorni=7):
    """Il lunedì mattina: segnala le fonti che da una settimana non portano nulla (feed cambiato o sito bloccato).
    Così l'elenco delle fonti resta buono nel tempo senza doverlo controllare a mano."""
    storico = [x.get("fonti", []) for x in (leggi_json(p) for p in giorni_salvati()[-giorni:]) if x]
    if len(storico) < giorni - 1:
        return
    storico.append(stato_oggi)
    vuote, rotte = [], []
    for f in stato_oggi:
        righe = [next((x for x in st if x.get("nome") == f["nome"]), None) for st in storico]
        righe = [r for r in righe if r]
        if len(righe) < giorni:
            continue                       # fonte aggiunta da poco: aspettiamo
        if all(not r.get("ok") for r in righe):
            rotte.append(f["nome"])
        elif all(r.get("ok") and not r.get("presi") for r in righe):
            vuote.append(f["nome"])
    if rotte:
        avvisa(f"Fonti non raggiungibili da {giorni} giorni (da sostituire in sources.json): {', '.join(rotte)}.")
    if vuote:
        avvisa(f"Fonti senza articoli da {giorni} giorni (feed cambiato o filtro troppo stretto?): {', '.join(vuote)}.")


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
    if oggi.weekday() == 0 and not (DATA / f"{giorno}.json").exists():
        controlla_salute_fonti(stato)
    chiave_t = lambda t: re.sub(r"\W+", "", (t or "").lower())[:80]

    # Seconda scansione della giornata? Se l'edizione di oggi esiste già (ed è venuta bene) questa diventa un
    # AGGIORNAMENTO: solo ciò che è uscito dopo, mostrato in cima alla pagina sopra l'edizione del mattino.
    file_oggi = DATA / f"{giorno}.json"
    mattino = leggi_json(file_oggi) if file_oggi.exists() else None
    aggiornamento = bool(mattino and mattino.get("riassunto") and "senza AI" not in str(mattino.get("modello"))
                         and "--nuova" not in sys.argv)
    n_agg = len(mattino.get("aggiornamenti", [])) + 1 if aggiornamento else 0
    if aggiornamento:
        print(f"L'edizione di oggi esiste già: preparo l'aggiornamento n. {n_agg} (solo novità).")

    # Niente doppioni: togliamo gli articoli già citati nelle ultime due settimane e diciamo a Gemini
    # quali strumenti e novità sono già stati segnalati (così uno strumento non torna ogni giorno).
    # Nell'aggiornamento togliamo anche TUTTI gli articoli già esaminati stamattina, anche se non citati.
    GIORNI_MEMORIA = 14
    soglia = (oggi - timedelta(days=GIORNI_MEMORIA)).strftime("%Y-%m-%d")
    recenti = [x for x in (leggi_json(p) for p in giorni_salvati()
                           if soglia <= p.stem < giorno or (aggiornamento and p.stem == giorno)) if x]
    gia_pubblicate = []
    vecchi_link, vecchi_titoli = set(), set()
    for x in recenti:
        for a in x.get("articoli", {}).values():
            vecchi_link.add(a.get("link"))
            vecchi_titoli.add(chiave_t(a.get("titolo")))
        for pre, r_ in riassunti_del_giorno(x):
            gia_pubblicate += [d.get("cosa", "") for d in r_.get("da_provare", []) + r_.get("osint", [])]
            gia_pubblicate += [n.get("titolo", "") for s in r_.get("sezioni", []) for n in s.get("notizie", [])]
    if aggiornamento:
        vecchi_titoli |= set(mattino.get("visti", []))
    gia_pubblicate = [t for t in dict.fromkeys(gia_pubblicate) if t][-150:]
    visti_oggi = [chiave_t(a["titolo"]) for a in articoli]       # da ricordare per l'aggiornamento
    prima = len(articoli)
    articoli = [a for a in articoli if a["link"] not in vecchi_link and chiave_t(a["titolo"]) not in vecchi_titoli]
    print(f"Tolti {prima - len(articoli)} articoli già visti" + (" stamattina o" if aggiornamento else "")
          + f" negli ultimi {GIORNI_MEMORIA} giorni.")

    def salva_aggiornamento(riassunto_u, modello_u, completi_u=0, salvati_u=None):
        mattino.setdefault("aggiornamenti", []).append({
            "generato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "modello": modello_u, "testi_completi": completi_u, "riassunto": riassunto_u})
        mattino.setdefault("articoli", {}).update(salvati_u or {})
        mattino["visti"] = list(dict.fromkeys(mattino.get("visti", []) + visti_oggi))
        file_oggi.write_text(json.dumps(mattino, ensure_ascii=False, indent=1), encoding="utf-8")

    if not articoli:
        if aggiornamento:
            print("Nessuna novità rispetto a stamattina.")
            salva_aggiornamento(None, "")
            rigenera_tutto()
            scrivi_avvisi()
            return
        avvisa("Tutti gli articoli erano già stati pubblicati: il sito non è stato aggiornato.")
        scrivi_avvisi()
        sys.exit(1)

    if aggiornamento:                     # id diversi da quelli del mattino (es. u1a3)
        for a in articoli:
            a["id"] = f"u{n_agg}{a['id']}"
    completi = arricchisci(articoli)
    candidati = sum(1 for a in articoli if a.get("tipo") in ("ufficiale", "testata", "minore"))
    if candidati >= 10 and completi == 0:
        avvisa("Non è stato possibile scaricare il testo di nessun articolo: il riassunto si basa solo sui titoli.")
    for a in articoli:
        a.setdefault("solo_titolo", not a.get("testo"))
    per_id = {a["id"]: a for a in articoli}

    def da_salvare(riassunto):
        usati = {i for s in riassunto["sezioni"] for n in s["notizie"] for i in n["fonti"]}
        usati |= {i for d in riassunto.get("da_provare", []) + riassunto.get("osint", []) for i in d["fonti"]}
        return {i: {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in per_id[i].items()}
                for i in usati}

    for d in (DATA, GIORNI):
        d.mkdir(parents=True, exist_ok=True)

    if aggiornamento:
        if not chiave:
            avvisa("GEMINI_API_KEY non impostata: aggiornamento saltato.")
            scrivi_avvisi()
            return
        try:
            riassunto, modello = chiedi_a_gemini(articoli, chiave, gia_pubblicate, aggiornamento=True)
            riassunto = verifica(riassunto, per_id)
        except Exception as ex:   # l'edizione del mattino resta com'è
            avvisa(f"Aggiornamento non riuscito, resta l'edizione del mattino ({str(ex)[:200]}).")
            scrivi_avvisi()
            return
        vuoto = not (riassunto["sezioni"] or riassunto.get("da_provare") or riassunto.get("osint"))
        salva_aggiornamento(None if vuoto else riassunto, modello, completi, {} if vuoto else da_salvare(riassunto))
        print("Aggiornamento: " + ("nessuna novità rilevante." if vuoto else
              f"{len(riassunto.get('da_provare', []))} strumenti, "
              f"{sum(len(s['notizie']) for s in riassunto['sezioni'])} novità."))
    else:
        modello = "senza AI"
        if chiave:
            try:
                riassunto, modello = chiedi_a_gemini(articoli, chiave, gia_pubblicate)
                riassunto = verifica(riassunto, per_id)
                if not (riassunto["sezioni"] or riassunto.get("da_provare") or riassunto.get("osint")):
                    raise RuntimeError("riassunto vuoto")
            except Exception as ex:  # qualunque problema: si pubblica comunque la versione semplice
                avvisa(f"Gemini non disponibile ({str(ex)[:200]}): pubblicata la versione senza riassunto.")
                riassunto, modello = senza_ai(articoli), "senza AI (Gemini non disponibile)"
        else:
            avvisa("GEMINI_API_KEY non impostata: pubblicata la versione senza riassunto.")
            riassunto = senza_ai(articoli)
        dati = {"giorno": giorno, "generato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "modello": modello, "testi_completi": completi, "riassunto": riassunto,
                "articoli": da_salvare(riassunto), "fonti": stato, "visti": visti_oggi}
        file_oggi.write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")

    forza = os.environ.get("SETTIMANA") == "1"
    if (oggi.weekday() == 6 and not (SETTIMANE / f"{giorno}.json").exists()) or forza:
        if chiave:
            try:
                costruisci_settimana(chiave, giorno)
            except Exception as ex:
                avvisa(f"Edizione della settimana non creata: {str(ex)[:200]}")

    rigenera_tutto()
    scrivi_avvisi()
    print(f"Fatto: {'aggiornamento' if aggiornamento else 'edizione'} del {giorno}.")


if __name__ == "__main__":
    main()
