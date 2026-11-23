# Fußball-Assist

**English summary:** Fußball-Assist is a Home Assistant custom integration that gives your Assist voice assistant (any LLM conversation agent that uses the Assist API) five tools for football questions in German: results and fixtures of a club, all matches of a day or matchday, league tables, top scorers, and the most interesting matches of the week. It covers the Bundesliga, 2. Bundesliga, 3. Liga, DFB-Pokal, Champions League, Europa League, Conference League, the German national team and the Frauen-Bundesliga. All data comes from [OpenLigaDB](https://www.openligadb.de) (ODbL 1.0). It deliberately does not answer questions about squads, coaches, transfers, TV, tickets or predictions. The tool descriptions, answers and prompts are in German. Requires Home Assistant 2026.10 or newer. Licensed under the Apache License 2.0.

## Was ist das?

Fußball-Assist erweitert den Sprachassistenten von Home Assistant (Assist) um fünf Werkzeuge für Fußballfragen. Ein Sprachmodell kann damit „Hat Bayern gestern gewonnen?“ oder „Wer führt die Torjägerliste an?“ nicht aus dem Gedächtnis, sondern anhand echter Daten beantworten. Die Integration legt keine Geräte und keine Entitäten an. Sie stellt dem Assistenten nur die Werkzeuge und eine kurze Anweisung zur Verfügung.

Die Daten kommen ausschließlich von OpenLigaDB. Abgedeckt sind:

- Bundesliga, 2. Bundesliga und 3. Liga
- DFB-Pokal
- Champions League, Europa League und Conference League
- die Nationalmannschaft der Männer (Länderspiele)
- die Frauen-Bundesliga (nur über die Werkzeuge für Spiele, Tabellen und Torjäger, nicht über Vereinsnamen)

Vereine werden unter ihrem Namen, Kurznamen, Kürzel oder Spitznamen erkannt („Bayern“, „BVB“, „Werkself“, „Fohlen“) und immer als Männermannschaft verstanden.

## Voraussetzungen

- Home Assistant 2026.10 oder neuer
- ein Sprachmodell als Konversationsagent, der die Assist-Schnittstelle nutzt (in den Einstellungen des Agenten muss unter „Home Assistant steuern“ die Schnittstelle „Assist“ gewählt sein, sonst sieht er die Werkzeuge nicht)
- für die Installation der Marketplace von Home Assistant (ab 2026.11 eingebaut, der Nachfolger von HACS) oder unter Home Assistant 2026.10 noch [HACS](https://hacs.xyz)
- eine GitHub-Verbindung im Marketplace beziehungsweise in HACS, weil Fußball-Assist nicht im Standardkatalog steht und über seinen Link hinzugefügt wird
- eine Internetverbindung von Home Assistant zu `api.openligadb.de`

## Installation

Seit Home Assistant 2026.11 ist HACS als **Marketplace** fest eingebaut; ein getrenntes HACS lässt sich dort nicht mehr installieren. Wer von einer älteren Version aktualisiert, behält seine über HACS installierten Inhalte: Der Marketplace übernimmt sie beim ersten Start.

### Home Assistant 2026.11 oder neuer: Marketplace

1. **Einstellungen > Marketplace** öffnen (nur für Administratoren sichtbar).
2. Oben rechts **Add from link** wählen.
3. Unter **GitHub link** `https://github.com/fsippel/fussball-assist` eintragen und **Add** wählen. Fragt der Marketplace nach dem Typ, **Integration** wählen. Dafür muss der Marketplace mit GitHub verbunden sein.
4. Die Seite von Fußball-Assist öffnen, **Install** wählen, die Version prüfen und noch einmal **Install** wählen.
5. Danach öffnet sich in der Regel gleich die Einrichtung. Verlangt Home Assistant stattdessen einen Neustart, erscheint dazu eine Meldung unter **Einstellungen > System > Reparaturen**, die den Neustart auch auslösen kann.

Die Bezeichnungen stammen aus der englischen Oberfläche und können in der deutschen anders lauten. Updates der Integration brauchen weiterhin einen Neustart.

### Home Assistant 2026.10: HACS

1. In Home Assistant HACS öffnen.
2. Oben rechts auf die drei Punkte tippen und **Benutzerdefinierte Repositories** („Custom repositories“) wählen.
3. Als Adresse `https://github.com/fsippel/fussball-assist` eintragen und als Typ **Integration** wählen. Dann hinzufügen.
4. Fußball-Assist in HACS suchen, **Herunterladen** wählen und die gewünschte Version installieren.
5. Home Assistant neu starten.

## Einrichtung

1. Unter **Einstellungen > Geräte & Dienste > Integration hinzufügen** nach „Fußball-Assist“ suchen.
2. **Wettbewerbe** wählen, zu denen Fragen beantwortet werden sollen. Vorgabe sind alle; mindestens einer muss gewählt sein.
3. Freiwillig einen **Hauptverein** und weitere **Lieblingsvereine** wählen. Der Hauptverein gilt, wenn in der Frage kein Verein vorkommt („Wie haben wir gespielt?“). Die Spiele der Lieblingsvereine stehen in der Wochenübersicht und in langen Spiellisten zuerst. Zur Auswahl stehen die Männermannschaften der gewählten Wettbewerbe.
4. Beides lässt sich später in den Optionen der Integration ändern.

Es gibt nur eine Einrichtung je Home Assistant. Beim Start lädt die Integration die Daten der gewählten Wettbewerbe im Hintergrund, mit Abstand zwischen den Abrufen. Wer in den ersten Minuten fragt, bekommt gegebenenfalls den Hinweis, dass noch nicht alles geladen ist. Danach wird nur abgerufen, wenn jemand fragt und die Daten alt genug sind; laufende Spiele werden höchstens einmal pro Minute aufgefrischt.

Jede Saison braucht eine eigene Datei mit der Zuordnung der Wettbewerbe. Enthalten ist die Saison 2026/27. Zur nächsten Saison ist ein Update der Integration nötig; fehlt es, zeigt Home Assistant eine Meldung unter **Reparaturen**, und die Werkzeuge sagen, dass für die neue Saison noch Daten fehlen.

## Welche Fragen man stellen kann

**Verein** (`fussball_assist__verein`): letzte und nächste Spiele eines Vereins oder der Nationalmannschaft, mit Gegner, Anstoß, Ergebnis, Torschützen, laufendem Stand und Pokalstatus.

- „Hat Bayern gestern gewonnen?“
- „Wann spielt Dortmund das nächste Mal zu Hause?“
- „Wann spielt Deutschland wieder?“

**Spiele** (`fussball_assist__spiele`): alle Spiele eines Tages, Wochenendes oder Spieltags, nach Wettbewerb geordnet. Gibt es keine, nennt das Werkzeug den nächsten Tag mit Spielen und bei einer Länderspielpause diese als Grund.

- „Läuft heute Abend noch irgendwo ein Spiel?“
- „Welche Spiele gibt es am Wochenende?“
- „Warum ist dieses Wochenende keine Bundesliga?“

**Tabelle** (`fussball_assist__tabelle`): Tabelle einer Liga oder der Ligaphase im Europapokal; zu einem Verein dessen Umfeld und fertig berechnete Punkteabstände. Für den DFB-Pokal und die Nationalmannschaft gibt es keine Tabelle. Wird ein Verein mit einer Liga genannt, in der er nicht spielt, zeigt das Werkzeug die Tabelle seiner Liga und sagt das dazu.

- „Auf welchem Platz steht Dortmund?“
- „Wie viele Punkte liegt Bayern vor dem Zweiten?“
- „Wer steht in der 2. Bundesliga ganz oben?“

**Torjäger** (`fussball_assist__torjaeger`): Torjägerliste eines Wettbewerbs oder die Tore eines bestimmten Spielers. Die Liste nennt keinen Verein; für die 3. Liga und die Conference League ist sie unvollständig, für die Nationalmannschaft gibt es keine.

- „Wer führt die Torjägerliste der Bundesliga an?“
- „Wie viele Tore hat Kane diese Saison schon?“
- „Wer sind die besten fünf Torjäger in der Champions League?“

**Woche** (`fussball_assist__woche`): höchstens fünf besondere Spiele dieser oder der nächsten Woche, jedes mit kurzem Grund, nach festen Regeln: Spiele der Lieblingsvereine, Länderspiele, K.-o.-Spiele im Pokal und Europapokal, Spitzenspiele und Derbys.

- „Was sind die wichtigsten Spiele diese Woche?“
- „Welche Spiele lohnen sich nächste Woche?“

## Was bewusst nicht beantwortet wird

Fußball-Assist liefert nur Ergebnisse, Termine, Tabellen, Torjäger und die Auswahl der Woche, und nur das, was in OpenLigaDB steht. Zu Kadern, Trainern, Transfers, Aufstellungen, Karten, Schiedsrichtern, Zuschauerzahlen, dem Spielort eines bestimmten Spiels, Fernsehübertragungen, Tickets, Spielabsagen, Auslosungs- und Finalterminen und aktuellen Titelträgern gibt es keine Daten, ebenso wenig zur aktuellen Spielminute und zur ewigen Bilanz zweier Vereine. Prognosen, Wahrscheinlichkeiten oder Tipps gibt der Assistent nicht ab. Damit das Sprachmodell dazu nicht aus veraltetem Wissen antwortet, bringt die Integration eine kurze Anweisung mit, die Home Assistant in die Anweisungen jedes Agenten mit Assist-Schnittstelle übernimmt: Er soll bei Fußballfragen die Werkzeuge nutzen, ehrlich sagen, wenn sie nichts liefern, bei den meisten der genannten Themen sagen, dass er es nicht weiß, und keine Prognosen abgeben. Das gilt für alle Assist-Pipelines, nicht nur für eine. Allgemeines Fußballwissen („Was ist Abseits?“) beantwortet das Sprachmodell weiterhin selbst.

Die Daten sind nicht live: Der Stand eines laufenden Spiels kann ein paar Minuten hinterherhängen, Tore kommen oft erst später in den Daten an, und Uhrzeiten nennt das Werkzeug nur, wenn die Quelle sie als feststehend führt.

## Datenquelle

Alle Daten stammen von [OpenLigaDB](https://www.openligadb.de) (`https://api.openligadb.de`), einem ehrenamtlich betriebenen Projekt, das unter der [Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/) steht. Die Integration ruft die Daten sparsam ab und hält sich an Mindestabstände. Die mitgelieferten aufgezeichneten Antworten in den Tests stammen ebenfalls von OpenLigaDB und stehen unter derselben Lizenz.

## Lizenz

Apache License 2.0, siehe [LICENSE](LICENSE).
