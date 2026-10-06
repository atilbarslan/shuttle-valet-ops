# Entscheidungen und Grenzen

[English](DECISIONS.md) · **Deutsch**

Warum manches so gebaut wurde, was das Produkt bewusst nicht tut und welche Grenzen bekannt sind.

Zurück zur [README](../README.de.md).

## Inhalt

1. [Warum ein Backend in einer einzigen Datei](#warum-ein-backend-in-einer-einzigen-datei)
2. [Warum kein Build-Schritt](#warum-kein-build-schritt)
3. [Ehrliche Messung](#ehrliche-messung)
4. [Was es nicht tut](#was-es-nicht-tut)
5. [Bekannte Grenzen](#bekannte-grenzen)

---

## Warum ein Backend in einer einzigen Datei

Das Backend steckt in einer einzigen `main.py`. Für einen einzelnen Entwickler war das Suchen und
Bearbeiten in einer Datei schneller als das Springen zwischen Modulen. Der Hauptnutzen einer
Aufteilung ist, dass mehrere Personen gleichzeitig daran arbeiten können; dieser Bedarf entstand
nie.

---

## Warum kein Build-Schritt

Es gibt pro Seite eine HTML- und eine JavaScript-Datei; kein Framework und kein Bundler. Die Datei,
die läuft, ist die Datei, die geschrieben wurde. Der Preis dafür ist doppelter Code
([Bekannte Grenzen](#bekannte-grenzen)).

---

## Ehrliche Messung

Jede Etappe eines Valet-Auftrags wird getrennt erfasst (bei der Abholung die Fahrt zum Kunden und
die Rückfahrt zur Werkstatt; bei der Zustellung die Vorbereitung in der Werkstatt und die Fahrt zum
Kunden). Bei Etappen mit Zielvorgabe werden die beim Start der Etappe berechnete Soll-Ankunftszeit
und die tatsächliche Ankunftszeit gespeichert; die Differenz bildet den Pünktlichkeitsbericht.

Doch „tatsächliche Ankunft“ ist in Wirklichkeit der Moment, in dem der Valet die Schaltfläche drückt.
Kommt der Valet an und drückt erst sechs Minuten später, fließt menschliche Verzögerung in die
Messung ein. Da die Gewohnheiten beim Drücken von Person zu Person verschieden sind, vermischt sich
diese Verzögerung genau mit dem Unterschied zwischen den Valets, den wir messen wollen.

Deshalb wird im Moment des Drückens auch die Entfernung des Valets zum Ziel erfasst. Gespeichert wird
keine Koordinate, sondern eine Entfernung in Metern. Da die Entfernung allein kein Beweis ist, werden
Alter und Genauigkeit der Standortmessung mitgespeichert. Der Bericht zählt aus der Ferne gedrückte
Einträge getrennt; ist die Messung veraltet oder ungenau, wird der Eintrag nicht zu Lasten der Person
gewertet.

Ob der Valet seinen Standort geteilt hat, wird getrennt markiert. Ein leeres Entfernungsfeld sagt
für sich genommen nichts aus; es kann auch die Zielkoordinate fehlen, etwa wenn der Kunde die
Standortfreigabe widerrufen hat.

---

## Was es nicht tut

- **Keine künstliche Intelligenz.** Die Routenreihenfolge entsteht aus geometrischen und
  heuristischen Regeln; Verkehrsdaten kommen von einer externen API. Solver-basierte Optimierung
  (OR-Tools und Ähnliches) ist in dieser Codebasis nicht enthalten.
- **Keine durchgehende Live-GPS-Verfolgung.** Ein Browser liefert im Hintergrund nicht laufend
  Standortdaten; iOS setzt die App aus, Android friert sie ein, und ein Service Worker hat keinen
  Zugriff auf den Standort. Der Standort wird bei jeder Aktion des Nutzers aktualisiert. Das Produkt
  zeichnet keine Live-Spur auf, sondern erzeugt Datensätze mit Zeitstempel und Ankunftsprognosen.
  Ein durchgehender Standort wäre nur mit einer nativen App möglich.
- **Keine SMS.** Links werden von Hand geteilt.
- **Keine native Mobil-App.** Alles läuft als Web und PWA.
- **Der Kunde sieht kein Fahrzeug, das sich auf einer Live-Karte bewegt,** sondern die
  Ankunftsprognose.

---

## Bekannte Grenzen

- **Das Backend ist eine einzige Datei.** Es wurde nicht aufgeteilt, weil es keinen zweiten
  Entwickler gab.
- **Die Testabdeckung ist unvollständig.** `tests/` enthält Unit-Tests für die reinen
  Hilfsfunktionen und Regeltabellen sowie Endpunkt-Tests für drei kritische Abläufe: die
  Valet-Statusübergänge (einschließlich zweier gleichzeitiger Anfragen), die Mandantentrennung und den
  Token-Widerruf. Die übrigen Endpunkte wurden von Hand am laufenden System getestet.
- **Die Integrationstests laufen nicht gegen Postgres.** Sie rufen die echten Endpunkte über HTTP
  auf, aber die Datenbank ist ein In-Memory-Client (`tests/fakes.py`), der das Verhalten nachbildet,
  auf das sich der Code verlässt: Filter, bedingte Updates und die Zeilen, die ein Schreibvorgang
  zurückgibt. Constraints, Trigger und SQL-Typen werden nicht geprüft.
- **CI, aber kein CD.** Jeder Pull Request und jeder Push auf `main` führt `ruff` und `pytest` in
  GitHub Actions aus. Die Auslieferung war manuell: Dateien wurden auf den Server kopiert und der
  Dienst neu gestartet.
- **Drei Abläufe sind nicht atomar** (siehe [Konsistenz](ARCHITECTURE.de.md#konsistenz-idempotenz-statt-atomarität)).
  Volle Atomarität bräuchte Stored Procedures.
- **Es gibt doppelten Code:** Die Valet-Stornierungsregeln sind auf dem Server und in den Skripten
  zweier Panels definiert, die Übergangsreihenfolge zusätzlich in den Schaltflächen des
  Valet-Bildschirms; die Route des Fahrers und die Reihenfolge der Fahrgäste werden an zwei
  getrennten Stellen auf dem Server berechnet; die Funktion zur Zeitanzeige ist in vier Dateien
  identisch; und das CSS jeder Seite steckt in ihrer eigenen HTML-Datei, sodass gemeinsame Stile
  wiederholt werden, statt in einem Stylesheet zu stehen. Ein Teil davon ist der Preis für den fehlenden Build-Schritt. Bei einer Änderung müssen
  alle Stellen gemeinsam geändert werden; sie sind im Code markiert.
- **Ein `401` beim ersten Senden kommt nicht in die Warteschlange.** Die Regel sagt, dass `401` in
  der Warteschlange bleibt, und beim Leeren der Warteschlange geschieht das auch. Ist die Sitzung aber
  beim ersten Senden eines Vorgangs bereits abgelaufen, leiten beide Feldbildschirme den Nutzer zur
  Anmeldung weiter, und dieser Vorgang kann verloren gehen.
- **Ein Audit-Befund wurde zurückgestellt:** Der Endpunkt zum Starten einer Route ändert den
  Zustand, wird aber mit `GET` aufgerufen. Da die Authentifizierung über ein `Bearer`-Token statt
  über ein Cookie läuft, entsteht dadurch keine CSRF-Lücke; die Umstellung auf `POST` wurde
  zurückgestellt, weil Client und Server gemeinsam geändert werden müssen. Eine Koordinatenprüfung
  wurde ergänzt.
- **Der Datenbank-Client ist synchron.** Seine Abfragen laufen jetzt in einem Thread-Pool und
  blockieren die Event-Loop nicht mehr; bei 50 gleichzeitigen Anfragen und simulierten 50 ms pro
  Abfrage stieg der Durchsatz von etwa 3-5 auf etwa 90-145 Anfragen pro Sekunde. Eine einzelne
  Anfrage wurde durch den Wechsel in den Thread etwa 5 % langsamer. Der asynchrone Supabase-Client
  würde diesen Wechsel überflüssig machen. Methode, Bedingungen und Grenzen der Messung:
  [docs/PERFORMANCE.md](PERFORMANCE.md).
- **Einige Prüfen-dann-Schreiben-Regeln sichert die Datenbank nicht ab.** „Ein aktiver Auftrag pro
  Valet“ und die Kontingentprüfungen lesen zuerst und schreiben danach, sodass zwei gleichzeitig
  eintreffende Anfragen beide durchkommen können. Die kritischen Statusänderungen sind durch eine
  Bedingung im Update selbst geschützt, diese Regeln nicht; ein partieller eindeutiger Index oder
  ein Constraint wäre die richtige Lösung.
- **Ein Teil des Zustands liegt im Prozessspeicher und setzt einen einzigen Worker voraus.** Die
  Liste der WebSocket-Verbindungen, der 30-Sekunden-Auth-Cache und die Zähler des Anfragelimits
  liegen im Speicher des Serverprozesses. Mit mehreren uvicorn-Workern würde eine Nachricht, die ein
  Worker sendet, die Clients eines anderen Workers nicht erreichen; ein auf einem Worker widerrufenes
  Token (Abmeldung, Deaktivierung der Firma) könnte aus dem Cache eines anderen Workers noch bis zu
  30 Sekunden akzeptiert werden; und jeder Worker würde die Anfragelimits für sich zählen, sodass das
  tatsächliche Limit mit der Zahl der Worker wüchse. Die richtige Lösung ist Redis Pub/Sub für
  Nachrichten und Cache-Invalidierung sowie Redis als Speicher für das Anfragelimit.
- **Die Mandantentrennung wird in jedem Endpunkt einzeln geprüft.** Die Begründung steht unter
  [Sicherheit und Datenschutz](SECURITY-PRIVACY.de.md#mandantentrennung): Jede Art von Ressource ist
  auf andere Weise an ihre Firma gebunden. Der Preis ist, dass die Prüfung in einem Endpunkt
  vergessen werden kann, und genau das hat das Audit gefunden. Robuster wäre eine gemeinsame
  FastAPI-Abhängigkeit, die die Firma der angefragten Ressource ermittelt und eine Abweichung ablehnt,
  bevor der Endpunkt läuft, oder Row-Level-Security-Richtlinien in Postgres (wofür das Backend sich
  auch nicht mehr mit dem Schlüssel verbinden dürfte, der sie umgeht).
