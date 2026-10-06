# Sicherheit und Datenschutz

[English](SECURITY-PRIVACY.md) · **Deutsch**

Identität und Mandantentrennung, die Sicherheitsmaßnahmen, was beim Ausfall einer Abhängigkeit geschieht und wie personenbezogene Daten nach dem KVKK behandelt werden.

Zurück zur [README](../README.de.md).

## Inhalt

1. [Identität, Berechtigungen und Isolation](#identität-berechtigungen-und-isolation)
2. [Sicherheit](#sicherheit)
3. [Fehlerbehandlung: was bei Ausfall durchlässt und was sperrt](#fehlerbehandlung-was-bei-ausfall-durchlässt-und-was-sperrt)
4. [KVKK: Datenhygiene von Anfang an](#kvkk-datenhygiene-von-anfang-an)

---

## Identität, Berechtigungen und Isolation

### Supabase, aber nicht Supabase Auth

Die Datenbank ist Supabase; die Authentifizierung ist unser eigenes JWT-System. Der Grund sind eine
Hierarchie mit sechs Rollen und die Mandantentrennung: Bei jeder Anfrage werden Rolle und
Unternehmen getrennt geprüft, und wir brauchten volle Kontrolle über den Token-Widerruf.

Das Backend verbindet sich mit dem Schlüssel mit den meisten Rechten zur Datenbank; Row Level
Security greift nicht, und alle Berechtigungsprüfungen liegen im Anwendungscode. Das ist bewusst
so: Die Berechtigungslogik ist nicht über Datenbank-Policies verstreut, sondern steht lesbar im
Anwendungscode.

### Rollenhierarchie

```
SUPER-ADMIN  >  ADMIN  >  DISPOSITION  >  SERVICEBERATER  >  FAHRER = VALET
                                                             KUNDE (kein Login, nur Token)
```

Ein Nutzer kann nur jemanden unterhalb seiner eigenen Ebene verwalten. Die einzige Ausnahme ist die
Delegation: Ein Admin kann einen weiteren Admin anlegen, dessen Zuständigkeit enger ist als seine
eigene (an eine Filiale oder eine Marke gebunden). So legt die Zentrale einen Filial-Admin an und
ein Filial-Admin einen Marken-Admin.

Fahrer und Valet sind Feldrollen auf derselben Ebene. Eine Route ansehen, starten und beenden,
Haltestellen-Vorgänge und Fahrgast-Vorgänge sind nur am eigenen Fahrzeug erlaubt.

Diese Regel kam erst später hinzu: Ein Audit ergab, dass ein Valet die Routenliste der
Shuttle-Fahrzeuge im eigenen Unternehmen abrufen und dabei personenbezogene Daten der Fahrgäste
sehen konnte. Beide Feldrollen wurden an dieselbe Prüfung „Ist das dein Fahrzeug?“ gebunden. Eine
spätere Durchsicht ergab, dass die drei Fahrgast-Endpunkte (abgeholt, abgesetzt, nicht erschienen)
weiterhin nur das Unternehmen prüften; ein Fahrer, der das Token eines Fahrgasts kannte, konnte also
einen Fahrgast eines anderen Fahrzeugs markieren. Sie nutzen jetzt dieselbe Prüfung.

### Mandantentrennung

Für jede Rolle außer Super-Admin wird eine Anfrage abgelehnt, wenn das Unternehmen der angefragten
Ressource nicht mit dem Unternehmen im Token übereinstimmt. Die Prüfung geschieht nicht in einer
einzigen Middleware, sondern in jedem Endpunkt, weil die Ressourcen verschieden sind (Auftrag,
Fahrzeug, Haltestelle, Route) und jede auf andere Weise mit ihrem Unternehmen verknüpft ist. Der
Preis: Sie kann in einem Endpunkt vergessen werden. Genau das fand das Audit: In einigen Endpunkten
fehlte die Prüfung, und ein Nutzer, der die ID eines Datensatzes eines anderen Unternehmens kannte,
konnte ihn lesen oder ändern (IDOR). Alle wurden auf dasselbe Muster gebracht.

Auch innerhalb eines Unternehmens gibt es Grenzen. Serviceberater und Disposition sehen nur
Datensätze ihrer eigenen Marke und können keine Datensätze für eine andere Marke anlegen. Ein an
eine Filiale gebundener Nutzer kann nur die Datensätze seiner Filiale ändern.

### Token-Lebensdauer und Widerruf

Tokens der Feldrollen (Fahrer, Valet) gelten 7 Tage, alle anderen 24 Stunden. Die lange Dauer im Feld
hat einen Grund: Wird ein Fahrer mitten in der Woche abgemeldet, steht die Arbeit still, und
Vorgänge in der Offline-Warteschlange können erst nach einer erneuten Anmeldung gesendet werden.

Der Widerruf deckt drei Fälle ab: einen einzelnen Token widerrufen (Abmelden), alle Tokens eines
Nutzers ungültig machen (Passwortänderung, Löschen eines Mitarbeiters) und ein Unternehmen
deaktivieren. Wird ein Nutzer oder Unternehmen gelöscht oder deaktiviert oder ein Passwort
zurückgesetzt, werden auch die zugehörigen Tokens widerrufen.

Anfangs wurde der Widerruf nach einer Passwortänderung oder dem Löschen eines Mitarbeiters nur in
Redis gehalten; wurde Redis geleert, wären widerrufene Tokens wieder gültig geworden. Der Widerruf
wird jetzt auch in der Datenbank gespeichert, und Redis ist nur noch ein Beschleuniger. Auch die
Tokens von Nutzern eines gelöschten Unternehmens blieben anfangs gültig; ein gelöschtes Unternehmen
gilt jetzt als inaktiv.

Zwei Stellen dekodierten das Token von Hand, statt die gemeinsame Prüfung zu durchlaufen, und
übersprangen dadurch einen Teil davon. Der WebSocket prüfte nur den Unternehmensstatus, sodass sich
ein abgemeldetes Token noch verbinden konnte. Ein Endpunkt für die Haltestellenliste, den sich das
Personal mit der Kundenseite teilt, ließ die nutzerbezogene Sperrfrist aus, sodass ein Token, das vor
einem Passwort-Reset ausgestellt worden war, ihn noch lesen konnte. Beide rufen jetzt dieselbe
Prüfung auf wie jeder andere Endpunkt.

Wie diese Prüfungen bei jeder Anfrage günstig gehalten werden, steht unter
[Performance](ARCHITECTURE.de.md#performance-weniger-redis-verkehr).

### Identität und Anzeigename sind getrennte Felder

Der Benutzername ist die Identität: Login, Token-Widerruf und jeder technische Abgleich nutzen ihn.
Der Anzeigename dient der Anzeige: der Name auf Bildschirmen und in Berichten, der türkische
Buchstaben und Leerzeichen enthalten darf.

Türkische Buchstaben sind in Benutzernamen absichtlich verboten. Die Eindeutigkeit wird nach dem
Umwandeln in Kleinbuchstaben geprüft, und die türkische Groß-/Kleinschreibung macht das
unzuverlässig: Aus `İ` wird beim Kleinschreiben `i` mit einem kombinierenden Punkt, während aus `I`
ein `i` wird. Das Ergebnis können zwei unterschiedlich aussehende Benutzernamen sein, die auf
dieselbe Identität fallen, oder zwei gleich aussehende Namen, die als verschiedene Identitäten
gelten, und keines von beidem löst einen Fehler aus.

Deshalb akzeptieren Benutzernamen nur ASCII und werden beim Eintippen in Formulare automatisch
umgewandelt. Anzeigenamen sind auch nicht eindeutig: In einem Unternehmen kann es zwei Personen
namens „Mehmet Yılmaz“ geben.

### Zeit

Alle Zeitstempel werden in UTC gespeichert. Die Panels übernehmen die Zeitzone nicht vom Browser,
sondern zeigen immer `Europe/Istanbul` an; ein im Ausland geöffnetes Panel zeigt also dieselbe
Uhrzeit.

---

## Sicherheit

Die Codebasis wurde zweimal vollständig auditiert. Die Befunde wurden nach Priorität geschlossen;
zwei wurden mit Begründung zurückgestellt (siehe [Bekannte Grenzen](DECISIONS.de.md#bekannte-grenzen)). Die
folgenden Entscheidungen stammen aus diesen Audits oder aus einem Problem im Produktivbetrieb.
Mandantentrennung und Token-Widerruf sind weiter oben behandelt, unter
[Identität, Berechtigungen und Isolation](#identität-berechtigungen-und-isolation).

### Ungenutzte Endpunkte wurden entfernt

Endpunkte, die kein Bildschirm aufrief, wurden entfernt statt repariert: zunächst vier, später ein
fünfter, ein Überbleibsel eines aufgegebenen Ansatzes zur durchgehenden Standortverfolgung (siehe
[Was es nicht tut](DECISIONS.de.md#was-es-nicht-tut)).

### Regeln gelten auf dem Server

Eine auf dem Bildschirm deaktivierte Schaltfläche lässt sich mit einer direkten API-Anfrage umgehen.
Deshalb werden Regeln wie „keine Fahrgast-Vorgänge vor dem Start der Route“ auch auf dem Server
durchgesetzt. Auch die Passwortregeln (Länge, Buchstaben und Ziffern) werden auf dem Server geprüft;
die Prüfung im Browser dient nur dem Komfort des Nutzers.

### Passwörter

Passwörter werden mit bcrypt gespeichert; neue Nutzer legen ihr Passwort über einen Einladungslink
selbst fest. Den Link erzeugt der Server, nicht die Oberfläche: Die Oberfläche sendet nur den
Benutzernamen, die Zieladresse wird aus der Datenbank gelesen. Einladungslinks gelten 7 Tage.

Hier gab es eine Falle, die mit dem Türkischen zusammenhängt: bcrypt akzeptiert keine Eingaben über
72 Byte, und türkische Buchstaben belegen in UTF-8 2 Byte. Ein Formular, das nach Zeichenzahl
begrenzt, konnte ein Passwort über dem Byte-Limit durchlassen, und der Server antwortete mit `500`.
Das Limit wird jetzt in Byte geprüft.

### Fehlermeldungen

Interne Fehlertexte der Datenbank werden nicht an den Client weitergegeben. Der Nutzer sieht eine
allgemeine Meldung, die Details gehen an das Fehler-Monitoring. Diese Regel entstand, nachdem bei
zwei Endpunkten Fehler mit Schemadetails direkt beim Client gelandet waren.

Serverseitige Meldungen laufen über Pythons `logging`-Modul, nie über `print`, und enthalten keine
personenbezogenen Daten: Eine versendete E-Mail wird als versendet protokolliert, ohne die Adresse.
Einträge der Stufe ERROR gehen zusätzlich an das Fehler-Monitoring. Wo eine Exception bewusst
ignoriert wird (etwa ein gescheiterter Cache-Schreibvorgang), steht im Code, warum das unbedenklich
ist.

### Audit-Log

Kritische Vorgänge wie Löschen, Passwort-Reset, Anlegen von Mitarbeitern, Zuweisen von Fahrzeugen
und Deaktivieren eines Unternehmens werden zusammen mit dem Ausführenden protokolliert; ebenso
fehlgeschlagene Anmeldeversuche. Das Log hält Ereignisse fest, keine Inhalte: Beim Passwort-Reset
wird das Passwort selbst, bei der Aktualisierung von Kontaktdaten werden die neuen Werte nicht
geschrieben; erfasst wird nur, welches Feld ausgefüllt wurde.

Die IP-Adresse im Audit- und im Einwilligungslog ist die, die der Reverse Proxy an den
Anwendungsserver meldet. Der Header `X-Forwarded-For` wird nicht direkt gelesen: Sein erster Eintrag
ist das, was der Client geschickt hat, und damit könnte jeder eine gefälschte Adresse ins
Einwilligungslog schreiben.

### Weitere Schutzmaßnahmen

Die automatische API-Dokumentation ist im Produktivbetrieb abgeschaltet. CORS erlaubt nur die
eigene Adresse der Anwendung. Fehlt eine der kritischen Umgebungsvariablen, bricht der Server beim
Start ab. Request-Bodies werden über typisierte Modelle entgegengenommen; Felder wie Rolle,
Auftragstyp, Fahrzeugtyp und Status werden gegen feste Listen geprüft, Namen und Kennzeichen gegen
Listen erlaubter Zeichen. Koordinaten durchlaufen eine Bereichsprüfung; (0, 0) wird bei gewählten
Standorten ebenfalls abgelehnt, weil es in der Praxis „Standort konnte nicht ermittelt werden“
bedeutet. An einer einzelnen Haltestelle können höchstens 100 Fahrgäste verarbeitet werden.

Der WebSocket hat eine Gesamtgrenze und eine Grenze pro Identität für Verbindungen; eine Verbindung,
die kein Token sendet, wird innerhalb von 10 Sekunden geschlossen. Ein Token des Personals durchläuft
dieselben Widerrufsprüfungen wie bei den REST-Endpunkten, sodass sich weder ein abgemeldetes oder
widerrufenes Token noch ein Nutzer eines deaktivierten Unternehmens verbinden kann.

Auf jeder Seite verbietet die Content Security Policy Inline-Skripte; die von der Kartenbibliothek
benötigte Lockerung ist nur auf Seiten mit Karte aktiv. Nutzerinhalte laufen durch DOMPurify, bevor
sie auf den Bildschirm geschrieben werden, und E-Mail-Inhalte werden HTML-escaped.

Die Endpunkte für Anmeldung und Passwortvergabe sind durch Anfragelimits pro Minute geschützt;
ebenso die wichtigsten Endpunkte der Kundenseite. Die Tokens in Kundenlinks sind 128-Bit-Zufallswerte.

---

## Fehlerbehandlung: was bei Ausfall durchlässt und was sperrt

Wenn Redis oder die Datenbank nicht erreichbar sind, lassen manche Prüfungen die Anfrage durch
(fail open) und manche stoppen sie (fail closed). Die Wahl hängt davon ab, was in welche Richtung
verloren geht.

| Prüfung | Wenn ihre Daten nicht lesbar sind | Grund |
|---|---|---|
| Durch Abmeldung widerrufenes Token | Durchlassen: das Token wird akzeptiert | Ein Ausfall darf nicht alle Mitarbeitenden im Feld aussperren |
| Sperrzeitpunkt nach Passwort-Reset oder Löschung | Durchlassen: das Token wird akzeptiert | Ebenso |
| Sperrzeitpunkt, der sich nicht auslesen lässt | Durchlassen: das Token wird akzeptiert | Ebenso |
| Abmeldung | Durchlassen: antwortet „abgemeldet“, auch wenn der Widerruf nicht gespeichert werden konnte | Die App verwirft das Token ohnehin |
| Unternehmen aktiv oder inaktiv | Redis ausgefallen: die Datenbank wird gelesen. Auch die Datenbank ausgefallen: die Anfrage endet mit einem Fehler. Ein nicht mehr existierendes Unternehmen gilt als inaktiv | Ein Ausfall ist kein Grund, ein nicht zahlendes Unternehmen zu bedienen |
| Lebensdauer eines Kundenlinks mit unlesbarem Datum | Durchlassen: der Link funktioniert | Ein falsches Datum darf den Kunden nicht von seinem eigenen Auftrag aussperren |
| Einwilligungsnachweis vor dem Speichern eines Standorts | Sperren: es wird kein Standort gespeichert | Der Verantwortliche muss die Einwilligung nachweisen können |
| Ob ein Unternehmen eine Einwilligung verlangt | Sperren: die Einwilligung wird abgefragt | Die sichere Voreinstellung ist, zu fragen |
| Einladungslink mit unlesbarem Datum | Sperren: der Link wird abgelehnt | Eine neue Einladung kostet wenig |
| Ablehnung und Widerruf der Einwilligung | Durchlassen: Ablehnung oder Löschung finden statt | Ein Protokollfehler darf das Recht des Kunden nicht blockieren |
| Audit-Log, Pünktlichkeits-Etappen | Durchlassen: die Aktion findet statt | Aufzeichnungen, keine Schranken |

**Warum die Token-Prüfungen durchlassen.** Ausgesperrt würden Fahrer und Valets mitten in einem
Auftrag; ein Infrastrukturausfall würde den Betrieb aller Unternehmen gleichzeitig stoppen. Das
Risiko ist begrenzt: Jedes Token ist signiert und läuft ab (24 Stunden für Büro-Rollen, 7 Tage für
Feld-Rollen), Rollen- und Unternehmensprüfungen hängen nicht von diesen Speichern ab, und die Lücke
besteht nur, solange die Datenbank nicht erreichbar ist und Redis keine zwischengespeicherte Antwort
für dieses Token hat. Das Risiko ist, dass ein abgemeldetes oder gestohlenes Token in dieser Zeit
weiter funktioniert.

**Warum die Einwilligung sperrt.** Nach dem KVKK liegt die Beweislast beim Verantwortlichen. Ein
ohne Einwilligungsnachweis gespeicherter Standort gilt als Verarbeitung ohne Einwilligung; der
schlimmste zulässige Ausgang ist daher „Einwilligung erfasst, kein Standort“, nie umgekehrt.

**In einem Bereich wie dem Bankwesen** würden die Token-Prüfungen sperren: Ein unbekannter
Widerrufsstatus hieße „ablehnen“, Tokens lebten Minuten statt Tage und würden über Refresh-Tokens
erneuert, der Widerruf läge in einem hochverfügbaren Speicher mit Alarmierung, und die Abmeldung
würde einen Fehler melden, statt Erfolg zu behaupten. Nutzer während eines Ausfalls auszusperren
ist dort ein akzeptierter Preis, weil eine missbrauchte Sitzung Geld bewegt.

---

## KVKK: Datenhygiene von Anfang an

Das KVKK ist das türkische Gesetz zum Schutz personenbezogener Daten.

Bei einem Valet-Auftrag wird der Standort des Kunden in dem Moment gelöscht, in dem er nicht mehr
gebraucht wird:

- Bei der Abholung, sobald der Valet das Auto vom Kunden übernimmt. Der Standort wird nicht mehr
  benötigt.
- Bei der Zustellung, sobald das Auto an den Kunden übergeben ist.

Widerruft der Kunde die Standortfreigabe, wird der Standort sofort gelöscht. Der Standort von
Shuttle-Fahrgästen wird zusammen mit Name, Telefonnummer und Kennzeichen aufbewahrt, weil er während
der laufenden Arbeit und in Berichten gebraucht wird; ein täglich laufender Datenbankjob löscht alle
Auftragsdatensätze, die älter als 30 Tage sind.

Kundenlinks haben eine begrenzte Lebensdauer: 12 Stunden beim Shuttle (die Fahrt endet am selben
Tag), 72 Stunden beim Valet (das Auto kann tagelang in der Werkstatt stehen). Das ist nur ein
Sicherheitsnetz; in erster Linie wird der Link ungültig, wenn die Arbeit erledigt ist. Es gibt zwei
Ausnahmen. Bei einer Valet-Abholung lebt der Link, bis der Zustellauftrag angelegt wird, damit der
Kunde sehen kann, dass das Auto in der Werkstatt ist. Nach einer Valet-Zustellung lebt der Link noch
24 Stunden, öffnet aber nur die Zufriedenheitsumfrage und liefert weder Name noch Telefonnummer,
Kennzeichen oder Standort; der Link erlischt, wenn die Umfrage abgeschickt ist oder die Zeit abläuft.

Jeder Endpunkt, den ein Kundenlink erreichen kann, setzt die Lebensdauer durch: die Tracking-Seite,
die Position in der Warteschlange, die Haltestellenliste, der WebSocket und beide
Standortübermittlungen. Anfangs tat das nur die Tracking-Seite, sodass ein abgelaufener Link noch
seinen Platz in der Warteschlange lesen oder sogar einen Standort übermitteln konnte. Das Ablehnen
und Widerrufen einer Einwilligung bleibt nach Ablauf bewusst möglich: Beides verringert nur die
gespeicherten Daten, und das Recht auf Widerruf soll nicht vom Alter eines Links abhängen.

Einwilligungsnachweise: Bei jeder Änderung der Datenschutzhinweise oder des Einwilligungstexts wird
die Textversion hochgezählt, und diese Version wird mit jeder Einwilligung gespeichert. So lässt sich
die Frage „Welchem Text hat diese Person zugestimmt?“ auch Jahre später noch beantworten. Wer
vergisst, die Version hochzuzählen, lässt alte Einwilligungen so aussehen, als wären sie für den
neuen Text erteilt worden.
