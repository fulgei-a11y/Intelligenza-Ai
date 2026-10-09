# AI News Digest

Ogni mattina alle 7:17 un riassunto in italiano delle notizie sull'intelligenza artificiale, con le fonti.
Sito: https://fulgei-a11y.github.io/Intelligenza-Ai/

## Come funziona

1. `digest.py` legge le fonti di `sources.json` (ultime 36 ore) e scarta gli articoli già usati il giorno prima.
2. Per gli articoli che hanno solo il titolo scarica il testo vero (anche dai link di Google News).
3. Gemini scrive il riassunto; regole fisse nel codice assegnano le etichette di affidabilità,
   segnalano le notizie basate "Solo titoli" e scartano accuse che arrivano solo da Reddit o siti minori.
4. La domenica nasce anche **La settimana dell'AI**, con le 5 notizie che contano.
5. `tools/audio.py` legge l'edizione con la voce Paola (MP3 tenuti per 60 giorni).
6. Le pagine in `docs/` hanno archivio con ricerca, glossario "Parole da sapere", anteprima per WhatsApp
   e si possono installare sul telefono come un'app ("Aggiungi a schermata Home").

## Se qualcosa non va

Il workflow apre una segnalazione nella scheda **Issues** del repository (GitHub ti avvisa via email) quando:
Gemini non risponde, molte fonti sono irraggiungibili, il testo degli articoli non si scarica, l'audio fallisce
o l'aggiornamento si interrompe.

## Comandi utili

- Actions → *Riassunto AI quotidiano* → **Run workflow**: rifà l'edizione di oggi.
  Scrivi `1` nel campo "settimana" per creare subito anche *La settimana dell'AI*.
- Per cambiare modello: Settings → Secrets and variables → Actions → Variables → `GEMINI_MODEL`.
