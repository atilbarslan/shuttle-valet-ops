# Architektur

[English](ARCHITECTURE.md) · **Deutsch**

Wie das System aufgebaut ist: die Code-Struktur, die Valet- und Shuttle-Abläufe, die Apps im Feld, Konsistenz und Performance.

Zurück zur [README](../README.de.md).

## Inhalt

1. [Projektstruktur](#projektstruktur)
2. [Valet-Ablauf: Zustandsautomat](#valet-ablauf-zustandsautomat)
3. [Shuttle: Routenreihenfolge und Ankunftsprognose](#shuttle-routenreihenfolge-und-ankunftsprognose)
4. [Bedingungen im Feld: PWA und Offline-Warteschlange](#bedingungen-im-feld-pwa-und-offline-warteschlange)
5. [Konsistenz: Idempotenz statt Atomarität](#konsistenz-idempotenz-statt-atomarität)
6. [Performance: weniger Redis-Verkehr](#performance-weniger-redis-verkehr)

---

## Projektstruktur

```
main.py                Das gesamte Backend: Endpunkte, Authentifizierung, Hilfsfunktionen
requirements.in        Direkte Abhängigkeiten
requirements.txt       Lock-Datei, aus requirements.in erzeugt (pip-tools)
requirements-dev.in    Entwicklungswerkzeuge: pytest, ruff, fakeredis, anyio
requirements-dev.txt   Lock-Datei, aus requirements-dev.in erzeugt
tests/                 Unit- und Endpunkt-Tests (In-Memory-Datenbank, fakeredis)
frontend/
  <seite>.html         Eine Seite pro Rolle
  js/<seite>.js        Das einzige Skript dieser Seite
  js/surum-kontrol.js  Hinweis „neue Version“ in den Panels ohne PWA
  config.js            Gemeinsame Einstellungen (API-Adresse, Abmelden)
  sw.js                Service Worker; einzige Quelle der Versionsnummer
  manifest.json        PWA-Manifest
  libs/purify.min.js
  fonts/
db/                    Basisschema, Migrationen und geplante Jobs (SQL)
scripts/load_test.py   Kleiner Lasttest (siehe docs/PERFORMANCE.md)
docs/                  Performance-Notizen und Bildschirmfotos
```

Die Seiten sind nach Rollen getrennt: Super-Admin, Unternehmens-Admin, Disposition,
Serviceberater, Fahrer, Valet, Kunde. Die Bildschirme für Fahrer und Valet sind PWAs; die übrigen
sind normale Web-Panels.

---

## Valet-Ablauf: Zustandsautomat

Ein Valet-Auftrag hat zwei Typen, und die Reihenfolge der Zustände hängt vom Typ ab. In beiden Typen
wartet der Auftrag in `BEKLIYOR_KONUM`, bis der Kunde über den Link seinen Standort markiert.

| Typ | Reihenfolge |
|---|---|
| Abholung (Haustür → Werkstatt) | `KONUM_ALINDI_VALE` → `VALE_YOLDA` → `ARAC_ALINDI` → `TAMAM_SERVIS` |
| Zustellung (Werkstatt → Haustür) | `KONUM_ALINDI_VALE` → `ARAC_ALINDI` → `VALE_YOLDA` → `TAMAM_MUSTERI` |

`VALE_YOLDA` bedeutet in beiden Typen dasselbe: Der Valet ist auf dem Weg zum Kunden. Die
Ankunftsprognose, die der Kunde sieht, beginnt genau in diesem Moment.

Anfangs gab es eine gemeinsame Reihenfolge. Bei der Zustellung bedeutete das, dass die
Ankunftsprognose begann, bevor das Auto die Werkstatt überhaupt verlassen hatte: Der Kunde sah etwa
„Valet in 12 Minuten an Ihrer Tür“, während das Auto noch auf dem Werkstattparkplatz stand. Die
Bedeutung jedes Zustands wurde festgehalten und die Reihenfolge nach Typ getrennt.

Die Übergänge sind in einem Dictionary definiert; ein nicht erlaubter Übergang wird vom Server mit
`409` abgelehnt. Die Schaltflächen auf dem Valet-Bildschirm folgen derselben Reihenfolge.

### Stornierung hängt vom Typ ab

Ein Auftrag kann nur storniert werden, bevor das Auto abgeholt ist. Die stornierbaren Zustände waren
zuerst eine flache Liste, die `VALE_YOLDA` enthielt. Bei der Abholung war das richtig: Das Auto war
noch nicht abgeholt. Bei der Zustellung bedeutete `VALE_YOLDA` jedoch, dass das Auto des Kunden in
den Händen des Valets und unterwegs war. Der Auftrag konnte trotzdem storniert werden, und weil eine
Stornierung das Auto zurück auf die Liste „wartet in der Werkstatt“ setzt, zeigte das Panel das Auto
auf dem Parkplatz, während es unterwegs war.

Das war dieselbe Fehlerklasse wie bei der Ankunftsprognose: Die flache Liste sah die umgekehrte
Reihenfolge der Zustellung nicht. Auch die Stornierungsregeln wurden in ein Dictionary pro Typ
umgewandelt. Die beiden Tabellen verweisen im Code aufeinander, damit eine Änderung an der einen
einen Blick auf die andere nach sich zieht.

### Ein Auftrag pro Valet zur selben Zeit

Der Valet-Bildschirm zeigt immer genau einen Auftrag. Solange ein zweiter Auftrag zugewiesen werden
konnte, erschien dieser nie auf dem Bildschirm und wartete in einer unsichtbaren Warteschlange.
Jetzt antwortet der Server mit `409`, wenn für einen Valet mit aktivem Auftrag ein neuer angelegt
werden soll. Die Prüfung hat keinen Datumsfilter: Auch ein unerledigter Auftrag von gestern zählt
als belegt.

### Warum ein Valet ein „Fahrzeug“ ist

Das Datenmodell ist fahrzeugzentriert; auf der Shuttle-Seite hängt alles an einem Fahrzeug. Deshalb
wird beim Anlegen eines Valet-Mitarbeiters im Hintergrund ein virtueller Fahrzeugdatensatz erzeugt.
Das ist ein Implementierungsdetail und erscheint auf keinem Bildschirm; der Valet wird überall mit
seinem Namen angezeigt. Wird ein Valet mit Vorgeschichte als Mitarbeiter gelöscht, bleibt sein
virtuelles Fahrzeug erhalten, weil vergangene Aufträge darauf verweisen; da kein Nutzer mehr dazu
gehört, fällt es aus den operativen Listen heraus.

---

## Shuttle: Routenreihenfolge und Ankunftsprognose

### Die Form einer Route

Im Haltestellenmodus wird jede Route klassifiziert, sobald der Admin sie abschließt. Verglichen wird
die Entfernung der mittleren und der letzten Haltestelle zur Zentrale:

- Liegt die letzte Haltestelle weiter entfernt als die mittlere, ist die Route linear (hin und
  zurück).
- Liegt die letzte Haltestelle näher als die mittlere, ist die Route halbmondförmig (ein Rundkurs).
- Eine Route mit zwei oder weniger Haltestellen gilt als linear.
- Ältere, nie klassifizierte Routen funktionieren weiter mit dem alten Verhalten; die
  Rückwärtskompatibilität bleibt erhalten.

### Reihenfolge

- **Linear:** zuerst das Absetzen von nah nach fern, dann das Abholen von fern nach nah. Dieselbe
  physische Haltestelle erscheint als zwei getrennte Karten, eine auf dem Hinweg und eine auf dem
  Rückweg. Mit nur einer Karte würde der Fahrer auf dem Hinweg einen Fahrgast abholen wollen, der für
  den Rückweg vorgesehen ist.
- **Halbmond:** alle Haltestellen in ihrer festgelegten Reihenfolge, unabhängig vom Auftragstyp.
- **Von Tür zu Tür:** Es gibt keine Haltestellen; die Fahrgäste werden nach Luftlinie zur Zentrale
  sortiert. Zuerst das Absetzen von nah nach fern, dann das Abholen von fern nach nah.

Diese Regeln sind eine eigene Heuristik auf Basis von Haversine-Entfernungen (Großkreis) zur
Zentrale. Es ist kein Solver und kein Optimierungsalgorithmus beteiligt.

Die Route, die der Fahrer sieht, und die Reihenfolge, die der Fahrgast sieht, werden nach denselben
Regeln an zwei getrennten Stellen berechnet. Im Code verweisen sie aufeinander; ändert sich die eine,
muss sich auch die andere ändern.

### Ankunftsprognose

Vom zuletzt bekannten Standort des Fahrzeugs aus wird eine kumulative Fahrzeit mit Verkehrsdaten bis
zu jeder kommenden Haltestelle berechnet, zuzüglich eines festen Zuschlags pro Haltestelle. Die
Prognose wird bei jeder Anfrage neu berechnet, nicht vom vorherigen Wert heruntergezählt. Fahrzeiten
zwischen Haltestellen werden 15 Minuten zwischengespeichert: Länger würde die Prognose veralten
lassen, wenn sich der Verkehr ändert, kürzer würde die Kosten der Karten-API erhöhen.

„Zuletzt bekannter Standort“ ist eine bewusste Formulierung. Es gibt keine durchgehende GPS-Verfolgung
(siehe [Was es nicht tut](DECISIONS.de.md#was-es-nicht-tut)); der Standort wird bei jeder Aktion des Fahrers
aktualisiert.

---

## Bedingungen im Feld: PWA und Offline-Warteschlange

Die Bildschirme für Fahrer und Valet sind PWAs. Ein Fahrzeug kann in einer Tiefgarage oder ohne
Netzabdeckung sein, und die Arbeit geht währenddessen weiter.

### Die Warteschlange

Schreibvorgänge laufen über einen einzigen Wrapper. Gibt es keine Verbindung oder tritt ein
Netzwerkfehler auf, wird der Vorgang in die Warteschlange gestellt und gesendet, sobald die
Verbindung zurück ist; der Fahrerbildschirm leert die Warteschlange außerdem beim Öffnen der App.
Vorgänge, die nur online möglich sind (etwa das Starten einer Route), werden offline gar nicht
angenommen.

### Die eine Regel der Warteschlange

Beim Leeren der Warteschlange wenden beide Bildschirme dieselbe Regel an:

> **In der Warteschlange bleiben nur Vorgänge, die später noch gelingen können.**

- `401` bleibt: Er gelingt, sobald sich der Nutzer erneut anmeldet.
- `5xx` und Netzwerkfehler bleiben: Sie sind vorübergehend.
- Andere `4xx`-Antworten werden verworfen: Sie sind eine endgültige Ablehnung durch den Server. Ein
  stornierter Auftrag, ein bereits weitergerückter Zustand oder eine geschlossene Route werden durch
  erneutes Versuchen nicht gültig.

Die beiden Bildschirme lagen in entgegengesetzten Richtungen dieser Regel falsch. Der eine
wiederholte jeden `4xx` endlos; die Aktualisierung eines stornierten Auftrags blieb in der
Warteschlange hängen. Der andere wertete beim Leeren jede Antwort außer `401` als „zugestellt“; bei
einem vorübergehenden `500` ging der Vorgang in der Warteschlange stillschweigend verloren. Das Leeren
wurde bei beiden auf diese Regel gebracht.

Dieselbe Regel gilt für das erste Senden eines Vorgangs: Ein vorübergehender Serverfehler im
Online-Betrieb stellt Fahrgast- und Haltestellen-Vorgänge in die Warteschlange. Der Fahrerbildschirm
hat eine bewusste Ausnahme: Anfragen zum Beenden der Route oder zum Ändern des Fahrzeugstatus kommen
nicht in die Warteschlange, wenn das erste Senden scheitert; dem Fahrer wird stattdessen ein Fehler
angezeigt. Eine verspätet aus der Warteschlange kommende Anfrage „Route beenden“ könnte eine neue
Route schließen, die der Fahrer inzwischen gestartet hat.

Beim letzten Fahrgast gab es außerdem ein Reihenfolgeproblem: Der Vorgang des Fahrgasts und die
Anfrage zum Beenden der Route gingen gleichzeitig hinaus. Wurde die Route zuerst geschlossen, wurde
der Vorgang des Fahrgasts abgelehnt, und der Fahrgast blieb unmarkiert. Jetzt wird zuerst der Vorgang
des Fahrgasts gesendet und abgewartet, danach wird die Route geschlossen.

### Zustand behalten

Die Routenliste des Fahrers wird im dauerhaften Speicher des Browsers gehalten. Früher lag sie im
Sitzungsspeicher, und die Liste ging verloren, wenn die App geschlossen und wieder geöffnet oder
offline neu geladen wurde. Die Liste wird beim Abmelden und am Ende der Route gelöscht, sodass keine
personenbezogenen Daten auf dem Gerät zurückbleiben.

### Updates auf das Gerät bringen

Der Service Worker liefert zuerst aus dem Cache aus; die App öffnet schnell und funktioniert offline.
Der Preis: Eine neue Version erreicht das Gerät nicht von selbst. Die Lösung hat drei Teile:

- Die Service-Worker-Datei wird mit Headern ausgeliefert, die dem Browser das Zwischenspeichern
  untersagen; der Browser (auch unter iOS) fragt den Server jedes Mal nach einer neuen Version.
- Übernimmt eine neue Version, lädt sich die Seite selbst neu.
- Die Versionsnummer wird unten auf dem Bildschirm angezeigt; ihre Quelle ist eine einzige Konstante
  im Service Worker.

Die Panels ohne PWA haben keinen Service Worker, ein offen gelassener Tab bekommt eine neue Version
also nie mit. Dort liest ein kleines Skript die Versionsnummer regelmäßig und blendet oben eine
Leiste „Neue Version veröffentlicht, bitte neu laden“ ein, wenn sie sich geändert hat. Ein
automatisches Neuladen gibt es bewusst nicht: Wer gerade ein Formular ausfüllt, soll seine Eingaben
nicht verlieren.

---

## Konsistenz: Idempotenz statt Atomarität

Drei Abläufe, die mehr als eine Tabelle aktualisieren (Route beenden, Fahrzeugstatus ändern,
Haltestellen-Vorgang abschließen), laufen nicht in einer Transaktion. Die Client-Seite von Supabase
bietet keine nativen Transaktionen; dafür wäre für jeden Ablauf eine eigene Stored Procedure nötig
gewesen.

Stattdessen wurde Idempotenz sichergestellt: Kommt dieselbe Anfrage zweimal, bleibt die zweite ohne
Wirkung. Das ist eine abgewogene Entscheidung. Wegen der Offline-Warteschlange ist es in diesen
Abläufen normal, dass derselbe Vorgang zweimal eintrifft; dass einer auf halbem Weg abbricht, ist
selten und lässt sich von Hand beheben. Der häufige Fall wurde zuerst geschlossen. Der Code benennt
diese Grenze in allen drei Abläufen ausdrücklich.

Es gibt eine Ausnahme: Das Hinzufügen und Löschen von Haltestellen geschieht atomar über eine in der
Datenbank definierte Funktion. Dort lag der Fall umgekehrt: Das Einfügen einer Haltestelle in der
Mitte erfordert das Verschieben der Reihenfolgenummern aller folgenden Haltestellen, und zwei
gleichzeitige Anfragen könnten dieses Verschieben durcheinanderbringen. Einfügen und Verschieben
geschehen zusammen in einer Datenbankfunktion, wodurch die Race Condition geschlossen ist; auch beim
Löschen wird an derselben Stelle neu nummeriert.

### Löschen und Fremdschlüssel

Überall, wo ein Fahrzeugdatensatz gelöscht wird, müssen die mit diesem Fahrzeug verknüpften
Auftragsdatensätze bedacht werden: Entweder werden sie zuerst gelöscht, oder der Vorgang wird mit
einer klaren Fehlermeldung abgelehnt. Diese Regel entstand nach einem `500` im Produktivbetrieb: Das
Löschen eines Valets mit Vorgeschichte scheiterte an der Fremdschlüssel-Bedingung der Datenbank.

---

## Performance: weniger Redis-Verkehr

Bei jeder Anfrage wurde mit drei getrennten Schlüsseln geprüft, ob ein Token widerrufen worden war:
das Token selbst, alle Tokens des Nutzers und das Unternehmen des Nutzers. Da die Bildschirme den
Server in Abständen abfragen, war die Zahl der Anfragen hoch, und jede Anfrage bedeutete drei
Redis-Befehle.

Das wurde in zwei Schritten gesenkt:

1. Die drei Schlüssel werden mit einem einzigen `MGET`-Befehl abgerufen.
2. Darüber liegt ein prozessinterner Speicher-Cache: Ein in den letzten 30 Sekunden geprüftes Token
   geht gar nicht mehr zu Redis.

Der zweite Schritt wirft eine Frage auf: Verzögert ein 30-Sekunden-Cache den Widerruf nicht um 30
Sekunden? Nein, denn Abmelden, Passwortänderung und Deaktivierung eines Unternehmens leeren den Cache
sofort. Der Cache beschleunigt nur den Fall, in dem sich nichts geändert hat.

In einem fünfminütigen Lasttest-Szenario sank die Zahl der Redis-Befehle von etwa 600 auf 230.
