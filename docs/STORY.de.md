# Die Geschichte hinter Shuttle & Valet Ops

## Die Fahrt, mit der es begann

Ich brachte mein Auto zur Wartung in ein Vertragsautohaus. Man bot mir ein Shuttle an, also fuhr ich damit bis in die Nähe meiner Wohnung. Als das Auto fertig war, hieß es, der Fahrer werde mich anrufen und dort abholen, wo ich abgesetzt worden war, und man nannte mir eine Abfahrtszeit.

Ich wartete genau eine Stunde. In dieser Stunde telefonierten der Fahrer und ich dreimal – „gleich da“, „bin unterwegs“. Da es ein Shuttle war, holte er auch andere Kunden ab. Nach einer Stunde rief er an und sagte, er sei angekommen. Er war nicht da; er war in die Straße hinter mir gefahren.

Auf dem Weg zum nächsten Kunden telefonierte er ständig, und zwei- oder dreimal hätten wir beinahe einen Unfall gehabt. Er hatte jemanden vergessen, also folgten weitere Anrufe und ein Umkehren. Alle im Wagen waren unzufrieden. Ich fragte den Fahrer, wie es wäre, wenn es eine App gäbe und alles automatisch angezeigt würde. „Das wäre großartig“, sagte er. Für mich begann das Projekt damit.

Geschäftlich war das zugleich mein größter Fehler: Ich begann zu bauen, weil jemand, der nicht der Entscheider war, die Idee gut fand. Weil ich das Problem selbst erlebt hatte, ergab für mich alles einen Sinn.

## Der Bau des Shuttle-Moduls

Zuerst entwarf ich die Architektur für das Shuttle: was es können musste und wie ich vorgehen sollte. Ich ging Schritt für Schritt vor und testete am Ende jedes Schritts. Die erste Version des Shuttle-Produkts dauerte etwa zwei Monate. Bis dahin nutzte ich KI nur, um meinen Code zu prüfen.

Dann kam die Inbetriebnahme, die ich noch nie gemacht hatte. Dort nutzte ich KI als Wegweiser – welche Werkzeuge ich nehmen und wie ich alles einrichten sollte.

## Die erste Präsentation

Als ich vom Produkt überzeugt war, ging ich direkt zurück zum Autohaus und hielt eine Präsentation vor dem Serviceleiter. Sie gefiel ihm sehr, aber auch er war nicht der Entscheider. Es war eine Unternehmensgruppe mit mehreren Marken; man schickte meine Präsentation an die Zentrale in Istanbul, und ich hörte nie wieder davon.

In der Zwischenzeit sprach ich weiter mit anderen Autohäusern, und dort spürte ich den ersten echten Schmerz: Nur sehr wenige betreiben überhaupt ein Shuttle, und die, die es tun, fuhren es aus Kostengründen zurück. Und niemand kaufte mir den Dienst ab. Inzwischen waren drei Monate vergangen.

## Auf der Suche nach einem Markt

Meine erste Idee war der Schülertransport: Eltern, Schule und Busunternehmen hätten alle sehen können, wo ein Schüler ist. Aber eine neue Vorschrift machte Ortungsgeräte in Schulfahrzeugen zur Pflicht, und die meisten dieser Geräte deckten – auch wenn sie schlechter waren als mein Produkt – das Wesentliche bereits ab, also wollte niemand zusätzlich bezahlen.

Ich kehrte zur Automobilbranche zurück. Shuttles waren selten, aber der Valet-Service (das Auto eines Kunden an der Haustür abholen und zurückbringen) war in Servicezentren viel verbreiteter. Um eine Demo in der Hand zu haben, baute ich das Valet-Modul auf meiner eigenen Shuttle-Architektur auf, diesmal mit starker KI-Unterstützung. Für das Shuttle hatte ich zwei Monate gebraucht; mit KI stand das Valet-System innerhalb von ein bis zwei Wochen zu etwa 90 %.

Ich traf mich wieder mit Unternehmen. Diesmal war das Hindernis die Gewohnheit. Nichts wurde aufgezeichnet; die Arbeit lief über Telefon und WhatsApp, und niemand war damit unzufrieden. Die ein, zwei Unternehmen, denen das Projekt gefiel, sagten, sie hätten kein Budget dafür. Ich sprach mit vielen weiteren Unternehmen, auch außerhalb meiner Stadt. Manchmal kam gar keine Antwort, und die Antworten, die kamen, waren negativ.

Als meine Hoffnung schwand, wies mich ein Freund auf die Logistik hin. Transportunternehmen, die mit Fahrern von Subunternehmern arbeiteten, riefen diese an, um zu erfahren, wo Fahrzeug und Ladung waren. Das konnte ich automatisieren, und mein System hätte den Güterverkehr mit relativ wenigen Änderungen abdecken können. Einige Teile des Codes hätten Arbeit gebraucht, aber bis dahin kein einziges Produkt verkauft zu haben, war zermürbend.

## Die Einstellung

Schon früh bewarb ich mich, weil ich an das Projekt glaubte, beim Ege Teknopark in İzmir. Der Zugang zu dessen F&E-Antragsportal dauerte Monate, sodass der vollständige Antrag im Juli 2026 eingereicht wurde, mit einem zweijährigen F&E-Plan: Ankunftszeitprognosen anhand des Verhaltens jedes einzelnen Fahrers zu personalisieren und Routen im Tagesverlauf neu zu planen, ohne den Fahrgästen bereits zugesagte Zeiten zu brechen. Das Projekt wurde angenommen, und der Vertrag stand im Oktober an. Doch ohne Einnahmen – nur ein, zwei Unternehmen hatten Interesse an einem kostenlosen Test – beschloss ich, die Firma nicht zu gründen, und unterschrieb nicht.

Insgesamt sprach ich mit mehr als 50 Unternehmen aus den Bereichen Autohäuser, Versicherungen, Pannenhilfe und Güterverkehr. Keines von ihnen hat bezahlt. Im Oktober 2026 habe ich das Produkt eingestellt.

## Was ich daraus gelernt habe

Dieses Projekt war eine der größten Erfahrungen meines Lebens. Ich habe alles allein gemacht, von der ersten Skizze bis zum Produktivserver. Es hat mich auf der Code-Seite viel gelehrt und auf der geschäftlichen Seite genauso viel:

- Vor dem Bauen mit der Person sprechen, die bezahlt. Die Begeisterung eines Fahrers ist keine Nachfrage.
- In vielen traditionellen Betrieben sind Telefon und WhatsApp „gut genug“; ein besseres Werkzeug muss ein Problem lösen, das den Käufer echtes Geld kostet.
- Interesse an einem kostenlosen Test ist nicht dasselbe wie Zahlungsbereitschaft.
- Die Architektur selbst in der Hand zu haben, hat sich gelohnt: Als das Shuttle fertig war, dauerte ein zweites Modul Wochen, nicht Monate.
