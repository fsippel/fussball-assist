# Aufgezeichnete Antworten von OpenLigaDB

Abgerufen am 17.11.2026 von `https://api.openligadb.de` (User-Agent `fussball-assist/0.1.0 (Home Assistant)`, 3 Sekunden Abstand zwischen zwei Abrufen). Die Dateien sind unverändert, nur `leagues` ist auf Einträge mit `leagueSeason == "2026"` gekürzt und `match_87778` ist ein einzelnes Spiel, ausgeschnitten aus `uel2026_2026`. Gespeichert ist kompaktes JSON (UTF-8).

Daten: OpenLigaDB (https://www.openligadb.de), lizenziert unter ODbL 1.0

| Datei | Abruf |
|---|---|
| `leagues` | `/getavailableleagues`, gekürzt auf `leagueSeason == "2026"` |
| `bl1_2026_md9` | `/getmatchdata/bl1/2026/9` |
| `bl1_2026_md10` | `/getmatchdata/bl1/2026/10` |
| `bl1_2026`, `bl2_2026`, `bl3_2026`, `uel2026_2026`, `uecl2026_2026`, `ffb1_2026` | `/getmatchdata/<Kürzel>/2026` (ganze Saisonliste) |
| `dfb_2026` | `/getmatchdata/dfb/2026` |
| `DFBN_2026` | `/getmatchdata/DFBN/2026` |
| `nla_2026` | `/getmatchdata/nla/2026` |
| `uecl2026_2026_md1` | `/getmatchdata/uecl2026/2026/1` |
| `ucl_2026` | `/getmatchdata/ucl/2026` |
| `ffb1_2026_md10` | `/getmatchdata/ffb1/2026/10` |
| `match_87778` | Spiel 87778 als einzelnes Objekt aus `uel2026_2026` (doppelte Ergebnisart) |
| `bl2_table_2026`, `bl1_table_2026`, `ucl_table_2026` | `/getbltable/<Kürzel>/2026` |
| `bl1_goalgetters_2026`, `uecl2026_goalgetters_2026` | `/getgoalgetters/<Kürzel>/2026` |
| `<Kürzel>_teams_2026` für `bl1`, `bl2`, `bl3`, `dfb`, `ucl`, `uel2026`, `uecl2026`, `ffb1` | `/getavailableteams/<Kürzel>/2026` |
| `lastchange_bl1_2026_9` | `/getlastchangedate/bl1/2026/9` |
| `empty` | `/getmatchdata/zzqqxx/2026` (unbekanntes Kürzel, Antwort `[]`) |
| `live_echt` | `/getmatchdata/bl2/2026/12`, abgerufen am 21.11.2026 um 13:12:45 Uhr während laufender Spiele |

Die Saisonlisten von `dfb`, `ucl`, `nla` und `DFBN` sind die Dateien `dfb_2026`, `ucl_2026`, `nla_2026` und `DFBN_2026`. Jede nicht aufgezeichnete Adresse liefert in den Tests `[]`.

Hinweis: `live_synthetisch.json` ist von Hand aus einem echten Spiel abgeleitet; die echte Aufzeichnung eines laufenden Spiels ist `live_echt.json` (siehe unten). Inhalt: Liste mit Spiel 83237 aus `bl1_2026_md10`, geändert sind nur `matchIsFinished: false`, `matchResults: []`, `lastUpdateDateTime` (`2026-11-20T21:38:12.4`) und zwei erfundene Einträge in `goals` (1:0 in Minute 23, 1:1 in Minute 51; `goalID` 900001 und 900002). Achtung: Die echten laufenden Spiele 85258 und 85259 in `nla_2026` zeigen eine andere Form, dort stehen Zwischenstände schon in `matchResults`.

`live_echt.json` ist eine echte Aufzeichnung: die ganze Antwort von `/getmatchdata/bl2/2026/12`, abgerufen am 21.11.2026 um 13:12:45 Uhr von `https://api.openligadb.de` (User-Agent `fussball-assist/messung (Live-Messung)`), unverändert, nur als kompaktes JSON gespeichert. Darin laufen 1. FC Nürnberg – Eintracht Braunschweig (83699, ein Tor zum 1:0 in der 10. Minute), Hannover 96 – SpVgg Greuther Fürth (83694) und VfL Wolfsburg – Holstein Kiel (83689, beide 0:0 ohne Tore). Laufende Spiele tragen den Zwischenstand in „Endergebnis“, und „Halbzeit“ steht schon in der ersten Halbzeit als 0:0 da.
