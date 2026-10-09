# AI News Digest

Ogni mattina alle 7:17 una guida in italiano ai **nuovi strumenti di intelligenza artificiale**:
cose utili da usare al lavoro e nella vita di tutti i giorni, con le fonti.
Sito: https://fulgei-a11y.github.io/Intelligenza-Ai/

## Cosa trovi ogni giorno

1. **🧪 Strumenti nuovi** — app e servizi AI appena usciti, usabili senza programmare. Ogni scheda dice
   a cosa serve (con un esempio), per chi è (lavoro o tutti i giorni), costo, piattaforma, se è in italiano,
   e ha il pulsante **Provalo** quando c'è il link diretto.
2. **Novità negli strumenti che già usi** — nuove funzioni di ChatGPT, Gemini, Claude, Copilot e simili.
3. **In breve** — al massimo 3 notizie davvero importanti, una riga ciascuna.
4. **Parole da sapere**, audio con la voce Paola, archivio con ricerca.
5. La domenica, **La settimana dell'AI**: i migliori strumenti e le novità più utili dei 7 giorni.

## Come funziona

1. `digest.py` legge le fonti di `sources.json`: Product Hunt (AI), Show HN, blog ufficiali di OpenAI e Google,
   ricerche mirate su Google News (in inglese e in italiano), testate tech, ANSA, Wired, Sole 24 Ore, Reddit.
   Scarta gli articoli già usati negli ultimi 14 giorni, così lo stesso strumento non torna ogni giorno.
2. Per gli articoli che hanno solo il titolo scarica il testo vero (anche dai link di Google News).
3. Gemini sceglie e spiega; regole fisse nel codice assegnano le etichette di affidabilità, tolgono i passi
   "come iniziare" quando le fonti non li descrivono e scartano accuse che arrivano solo da siti minori.
4. `tools/audio.py` legge l'edizione con la voce Paola (MP3 tenuti per 60 giorni).

## Se qualcosa non va

Il workflow apre una segnalazione nella scheda **Issues** del repository (GitHub ti avvisa via email) quando:
Gemini non risponde, molte fonti sono irraggiungibili, il testo degli articoli non si scarica, l'audio fallisce
o l'aggiornamento si interrompe.

## Comandi utili

- Actions → *Riassunto AI quotidiano* → **Run workflow**: rifà l'edizione di oggi.
  Scrivi `1` nel campo "settimana" per creare subito anche *La settimana dell'AI*.
- Il modello è scelto in automatico (gemini-3.5-flash, con riserve). La variabile `GEMINI_MODEL`
  (Settings → Secrets and variables → Actions → Variables) serve solo per forzarne uno: di norma va lasciata vuota.
