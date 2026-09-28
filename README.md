# analyst-snapshot

Raccolta notturna di rating e revisioni analisti. Gira su GitHub Actions — gratis,
con il computer spento, e con la rete libera che il cloud Anthropic non ha.

## Setup (una volta, ~5 minuti)

1. Crea un repo **pubblico** su GitHub (i dati sono tutti pubblici, nessun segreto qui).
2. Copia dentro questi file e fai push.
3. In locale o via Actions, genera l'universo:
   `pip install pandas lxml html5lib && python build_universe.py && git add universe.csv && git commit -m universo && git push`
4. Su GitHub: **Actions → analyst-snapshot → Run workflow** per il primo giro a mano.
5. Da lì in poi parte da solo alle 05:30 UTC nei giorni feriali.

## Universo

S&P 500, FTSE 100, FTSE 250, FTSE MIB, DAX 40, CAC 40, Euro Stoxx 50 (`build_universe.py`,
workflow 1). `enrich_universe.py` aggiunge nome, ISIN, valuta e borsa per la ricerca nel terminale.

## Cosa produce

    data/ratings/YYYY-MM-DD.csv     rating (scala 1-5) + target price dell'analisi (pt_from, pt_to)
    data/revisions/YYYY-MM-DD.csv   conteggi revisioni EPS a 30 giorni
    data/targets/YYYY-MM-DD.csv     target di consenso (medio, mediano, min, max)
    data/prices/YYYY-MM-DD.csv      chiusure giornaliere (primo giro: 1 anno di storico)
    data/latest_health.csv          esito del run: ticker falliti, grade non mappate

Append-only: ogni notte un file nuovo, mai riscritto. È il punto — la storia
point-in-time è l'unica cosa che non puoi ricomprare dopo.

`build_db.py` (workflow 3-terminale) ricostruisce ogni notte il database SQLite e il
terminale web su GitHub Pages.

## Investitori istituzionali (workflow 4)

- `inst_13f.py`: portafogli 13F dai **SEC Form 13F Data Sets** (dal 2013), poi ogni 3 ore i depositi nuovi da EDGAR.
- `inst_map.py`: CUSIP → ticker (ISIN dell'universo, poi OpenFIGI) e prezzi settimanali rettificati.
- `inst_shorts.py`: posizioni corte nette europee (CONSOB, AMF per detentore; FCA aggregato).
- `inst_score.py` (nel workflow 3): score "chi seguire" con test fuori campione, pagine Investitori, gestore e "chi possiede".
- Prima volta: Actions → 4-istituzionali → Run workflow → `backfill`. Poi gira da solo.
- Secrets facoltativi: `OPENFIGI_KEY` (mappatura più rapida), `NTFY_TOPIC` (notifiche push sul telefono con l'app ntfy), `SEC_UA`.
- I 13F non contengono short né liquidità: la liquidità è mostrata come n.d.

## Nota sui costi

Actions è gratis illimitato sui repo pubblici. Su repo privato consuma dal monte
di 2.000 minuti/mese: ~20 min × 22 giorni = ~440 min, ci sta comunque.
