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

## Cosa produce

    data/ratings/YYYY-MM-DD.csv     rating con scala 1-5 normalizzata
    data/revisions/YYYY-MM-DD.csv   conteggi revisioni EPS a 30 giorni
    data/latest_health.csv          esito del run: ticker falliti, grade non mappate

Append-only: ogni notte un file nuovo, mai riscritto. È il punto — la storia
point-in-time è l'unica cosa che non puoi ricomprare dopo.

## Nota sui costi

Actions è gratis illimitato sui repo pubblici. Su repo privato consuma dal monte
di 2.000 minuti/mese: ~20 min × 22 giorni = ~440 min, ci sta comunque.
