# AI News Digest 🤖📰

Ogni mattina un riassunto in italiano delle notizie sull'intelligenza artificiale,
pubblicato come pagina web gratuita su GitHub Pages.

**Come funziona:** GitHub Actions avvia `digest.py` ogni giorno alle 7:17 (ora italiana).
Lo script legge una ventina di fonti (laboratori AI, testate tech, testate italiane,
ricerca, community), tiene solo gli articoli delle ultime 36 ore e chiede a Gemini
di scrivere il riassunto. Ogni notizia riporta i link agli articoli originali.

**Affidabilità:**
- Il modello può citare solo articoli realmente scaricati: le notizie senza una fonte valida vengono scartate, quindi niente link inventati.
- Ogni notizia ha uno o più link all'articolo originale per verificare.
- In fondo alla pagina c'è l'elenco delle fonti raggiunte e di quelle non disponibili quel giorno.
- Se Gemini non risponde, la pagina viene pubblicata lo stesso con i soli titoli e link.

---

## Installazione (circa 10 minuti, tutto gratis)

### 1. Crea il repository
1. Su GitHub clicca **New repository**, chiamalo ad esempio `ai-news-digest` e scegli **Public**: GitHub Pages è gratis solo sui repository pubblici.
2. Carica tutti i file di questa cartella con **Add file → Upload files**, compresa la cartella `.github`.
   > Attenzione: la cartella `.github` è nascosta su Mac e Windows. Se non riesci a caricarla, crea il file a mano con **Add file → Create new file**, nome `.github/workflows/digest.yml`, e incolla il contenuto.

### 2. Ottieni la chiave Gemini gratuita
1. Vai su https://aistudio.google.com/apikey e accedi con il tuo account Google.
2. Clicca **Create API key** e copia la chiave.

### 3. Salva la chiave nel repository
**Settings → Secrets and variables → Actions → New repository secret**
- Name: `GEMINI_API_KEY`
- Secret: la chiave copiata

### 4. Attiva la pagina web
**Settings → Pages** → Source: **Deploy from a branch** → Branch: `main`, cartella `/docs` → **Save**.

### 5. Primo avvio
**Actions → Riassunto AI quotidiano → Run workflow**.
Dopo 1-2 minuti il riassunto sarà su `https://TUO-NOME-UTENTE.github.io/ai-news-digest/`.
Ti consiglio di salvarlo nella schermata Home del telefono.

---

## Personalizzare

| Cosa | Dove |
|---|---|
| Aggiungere o togliere fonti | `sources.json` (qualsiasi feed RSS/Atom) |
| Orario | riga `cron` in `.github/workflows/digest.yml`, in ora UTC |
| Stile e contenuto del riassunto | testo `ISTRUZIONI` in `digest.py` |
| Modello Gemini | variabile `GEMINI_MODEL` in Settings → Secrets and variables → Actions → **Variables** (default `gemini-2.5-flash`) |

## Note
- **TikTok non è incluso**: non offre feed pubblici e i video non si possono riassumere in modo affidabile come testo. Come voce "social" ci sono Hacker News e Reddit, segnalati nel riassunto come post di community e non come fonti verificate.
- **Piano gratuito di Gemini**: Google può usare i testi inviati per migliorare i suoi modelli. Qui si inviano solo notizie pubbliche, quindi non è un problema.
- Se una fonte risulta spesso "non raggiungibile" in fondo alla pagina, sostituisci il suo indirizzo in `sources.json`.
- Per provarlo sul tuo computer: `GEMINI_API_KEY=la_tua_chiave python3 digest.py`, poi apri `docs/index.html`. Serve Python 3.9 o successivo, nessun pacchetto da installare.
