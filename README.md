# funkSTONKS

Persönliche Ausgabenübersicht für eine Person: Kontoauszug, Kreditkarte,
PayPal, Splitwise und Depot importieren, automatisch verrechnen und
kategorisieren, den Rest von Hand zuordnen – Dashboard, Jahresübersicht,
Vergleich, laufende Kosten, Investitionen. Läuft nur auf deinem Rechner,
alle Daten bleiben bei dir.

## Erster Start (Windows)

1. **Herunterladen:** oben auf den grünen Knopf **Code** klicken, dann
   **Download ZIP**. Die ZIP-Datei entpacken (Rechtsklick → „Alle
   extrahieren“), zum Beispiel nach *Dokumente*. Den Ordner behalten – darin
   liegen später deine Daten.
   *(Wer Git hat: `git clone` mit der Adresse dieser Seite geht genauso.)*
2. **Starten:** im Ordner **`run.bat`** doppelklicken. Ein schwarzes Fenster
   geht auf. Fehlt Python, fragt es „Jetzt automatisch installieren? [J/N]“
   – **J** drücken, die Installation läuft von selbst. Danach richtet es
   sich einmalig ein (etwa eine Minute) und öffnet den Browser mit
   funkSTONKS.
3. **Einrichten:** die erste Seite fragt, welche Quellen du nutzt (Konto,
   Karte, PayPal, Splitwise). Alles ist später unter *Einstellungen*
   änderbar. Hilfe steht in der App unter *Hilfe*.
4. **Ab dann:** im Ordner liegt die Verknüpfung **funkSTONKS** –
   doppelklicken, fertig. Schließt du das Browserfenster, beendet sich die
   App von selbst.

**Mac:** statt `run.bat` die Datei `run.command` doppelklicken (beim ersten
Mal Rechtsklick → Öffnen, weil sie nicht signiert ist).

**Updates:** beim Start sieht die App nach, ob es auf GitHub eine neuere
Version gibt, und zeigt dann oben ein Banner – „Jetzt aktualisieren“
spielt sie ein und startet neu, deine Daten bleiben. Unter *Einstellungen →
Start und Updates* kannst du jederzeit nachsehen oder die Prüfung
abschalten.

**Deine Daten** liegen in der Datei `moneyflow.db` im Ordner. Unter
*Sicherung* legst du Kopien an und spielst sie zurück; nichts verlässt
den Rechner, außer der Versionsprüfung und – wenn eingeschaltet – den
Depotkursen für deine eigenen Positionen.

## Fertige Version ohne Python (Windows)

Die [neueste Version](../../releases/latest) hat eine ZIP-Datei mit
`funkSTONKS.exe`. Entpacken, EXE starten – der Browser öffnet sich, die
Datenbank entsteht neben der EXE. Windows SmartScreen warnt beim ersten
Start, weil die Datei nicht signiert ist: „Weitere Informationen“ →
„Trotzdem ausführen“.

## For developers

Flask + SQLite, package `funkstonks/`, entry point `app.py`, templates in
`templates/`, charts with a local copy of Apache ECharts. `run.bat` /
`run.command` create `.venv` and install Flask; `build.bat` / `build_mac.sh`
produce the stand-alone app in `dist/funkSTONKS`. Tests:

```
.venv\Scripts\python.exe tests\run_all.py
```

Every suite runs against a temporary database. The user interface is German.
