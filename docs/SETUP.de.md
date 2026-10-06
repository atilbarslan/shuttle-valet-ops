# Einrichtung

[English](SETUP.md) · **Deutsch**

Wie man die Anwendung startet, die Datenbank einrichtet und die Tests ausführt.

Zurück zur [README](../README.de.md).

## Inhalt

1. [Einrichtung](#einrichtung)

---

## Einrichtung

Voraussetzungen: Python 3.12, ein Supabase-Projekt (Postgres), Redis, ein Mapbox-Konto und ein
Resend-Konto für den E-Mail-Versand.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # Werte eintragen
uvicorn main:app --reload
```

Die Abhängigkeiten stehen in zwei Dateien: `requirements.in` listet nur die Pakete, die der Code
direkt importiert, und `requirements.txt` ist die daraus mit pip-tools erzeugte Lock-Datei, die jedes
Paket einschließlich der indirekten festlegt; installiert wird aus der Lock-Datei. Manche indirekten
Pakete wirken fremd: `pyiceberg` etwa kommt über `supabase` → `storage3` herein. Um eine
Abhängigkeit zu ändern oder zu aktualisieren, `requirements.in` bearbeiten und
`pip-compile --strip-extras requirements.in` ausführen (`pip install pip-tools`; mit `--upgrade` für
neuere Versionen). `requirements-dev.in` und `requirements-dev.txt` funktionieren für die
Test-Werkzeuge genauso; sie nach `requirements.txt` erzeugen, da sie an deren Versionen gebunden sind.

`.env` erwartet den JWT-Signaturschlüssel, die Schlüssel für Datenbank, Redis, Karten- und
E-Mail-Anbieter sowie die Adresse der Anwendung (`BASE_URL`); jeder Eintrag ist in `.env.example`
erklärt. Fehlt einer, startet der Server nicht. Beginnt `BASE_URL` mit `https://`, läuft die
Anwendung im Produktivmodus und lässt lokale Adressen über CORS nicht zu; lokal
`http://localhost:8000` verwenden.

Das Backend liefert den Ordner `frontend/` selbst aus; die Anwendung öffnet sich unter
`http://localhost:8000`. Das Frontend ruft die API unter der Adresse auf, von der die Seite geöffnet
wurde, eine separate Einstellung ist nicht nötig.

Für die Kartenseiten steht das browserseitige Mapbox-Token als Platzhalter in `frontend/js/admin.js`
und `frontend/js/musteri.js`, die Absenderadresse für E-Mails in `main.py`; durch eigene Werte
ersetzen. Die Content-Security-Policy-Zeile oben in jeder Seite unter `frontend/` erlaubt
`https://YOUR_DOMAIN` und `wss://YOUR_DOMAIN`; `YOUR_DOMAIN` durch die Domain ersetzen, unter der
die Anwendung läuft.

Datenbank: `db/00_temel_sema.sql` einmal im SQL-Editor von Supabase ausführen; die Datei legt alle
Tabellen und die Funktionen zum Einfügen und Löschen von Haltestellen an. Danach die Erweiterung
`pg_cron` aktivieren und die geplanten Bereinigungsjobs in `db/kvkk_temizlik.sql` (zwei Jobs),
`db/kvkk_riza.sql` und `db/memnuniyet_anketleri.sql` ausführen. Die übrigen Dateien unter `db/` sind
Migrationen für ältere Datenbanken und bereits im Basisschema enthalten.

Die Seiten mit den Datenschutzhinweisen sind nicht in diesem Repository enthalten, weil sie die
rechtlichen Angaben eines realen Unternehmens enthielten. Die Kundenseite verlinkt auf
`frontend/kvkk/aydinlatma-yolcu.html`, die Passwortseite auf `frontend/kvkk/aydinlatma-personel.html`;
eigene Hinweise unter diesen Pfaden ablegen.

Im Produktivbetrieb die Anwendung hinter einem Reverse Proxy (etwa nginx) auf demselben Rechner
betreiben. Der Proxy muss `X-Forwarded-For` senden; uvicorn vertraut diesem Header standardmäßig nur
von 127.0.0.1 (`--forwarded-allow-ips`) und reicht die echte Client-Adresse an die Anwendung weiter,
wo die Anfragelimits sowie das Audit- und das Einwilligungslog sie verwenden.

Das Fehler-Monitoring ist optional: Es ist aktiv, wenn `SENTRY_DSN` gesetzt ist, sonst abgeschaltet.

Tests: `pip install -r requirements-dev.txt`, danach `pytest`. Sie verwenden Platzhalter-Einstellungen
und brauchen weder Datenbank noch Redis noch einen externen Dienst: Supabase wird durch einen
In-Memory-Client ersetzt, Redis durch fakeredis. Die Unit-Tests decken Entfernungen, Eingabeprüfung,
die Rollenhierarchie, die Lebensdauer von Kundenlinks, den Valet-Zustandsautomaten mit seiner
Stornierungsregel, Token-Lebensdauern und Passwort-Hashing ab. Die Endpunkt-Tests decken die
Valet-Statusübergänge, die Mandantentrennung und den Token-Widerruf ab.
