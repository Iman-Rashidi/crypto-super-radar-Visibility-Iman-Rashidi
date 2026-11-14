# Grundlagen der technischen Analyse — Easy Crypto Engine

## Zweck und Einordnung

Die technische Analyse bewertet einen handelbaren Markt anhand historischer und aktueller Marktdaten, insbesondere Preis, OHLCV-Kerzen und Handelsvolumen. Im CRYPTO SUPER RADAR Easy Engine dient sie dazu, kurzfristige bis mittelfristige Marktbewegungen zu beschreiben, Trends und mögliche Wendepunkte zu erkennen und Handelsgelegenheiten schnell zu bewerten.

Technische Indikatoren sind mathematische Auswertungen von Preis- und Volumendaten. Chartmuster beschreiben wiederkehrende Preisformationen. Beide liefern Hinweise und Wahrscheinlichkeiten, keine sicheren Vorhersagen.

Diese Grundlagen sind eine Wissens- und Dokumentationsschicht. Sie erzeugen **keine zusätzlichen Pflicht-Gates** und dürfen den Easy-Entscheidungspfad nicht allein aufgrund einer abweichenden Interpretation blockieren.

## Zentrale Begriffe

### Trend
Ein Trend beschreibt die vorherrschende Kursrichtung:
- Aufwärtstrend: aufeinanderfolgende höhere Hochs und höhere Tiefs.
- Abwärtstrend: aufeinanderfolgende tiefere Hochs und tiefere Tiefs.
- Seitwärtsphase: keine stabile Folge höherer oder tieferer Extrempunkte.

Der Easy Engine soll Richtung und Stärke mit einer schlanken Kombination aus Preisstruktur und gleitenden Durchschnitten erfassen. Ein einzelner Indikator darf nicht als vollständiger Trendnachweis gelten.

### Unterstützung und Widerstand
- Unterstützung: Preiszone, in der Kaufinteresse einen weiteren Rückgang bremsen kann.
- Widerstand: Preiszone, in der Verkaufsinteresse einen weiteren Anstieg bremsen kann.

Es handelt sich um Zonen und nicht um garantiert exakte Preislinien. Der Motor soll Zonen aus nachvollziehbaren Swing-Hochs/-Tiefs und wiederholten Reaktionen ableiten. Ein Ausbruch kann echt sein oder scheitern (False Breakout).

### Gleitende Durchschnitte
Moving Averages glätten Preisreihen:
- SMA: einfacher arithmetischer Durchschnitt.
- EMA: stärkeres Gewicht für jüngere Preise.

Sie können Trendrichtung, Dynamik und mögliche dynamische Unterstützungs-/Widerstandszonen beschreiben. Parameter und Timeframe müssen im Signal offengelegt werden.

### Technische Indikatoren
Gängige Indikatoren:
- RSI: misst die relative Stärke jüngster Aufwärts- und Abwärtsbewegungen.
- MACD: vergleicht gleitende Durchschnitte und beschreibt Momentum sowie mögliche Wechsel.
- Stochastik-Oszillator: vergleicht den Schlusskurs mit der Handelsspanne eines gewählten Rückblickfensters.
- ATR: misst die durchschnittliche Schwankungsbreite und unterstützt die Volatilitäts- und Stop-Distanz-Schätzung.
- Volumen: hilft, die Beteiligung hinter einer Preisbewegung einzuschätzen.

Indikatoren sind Transformationen derselben Marktdaten und deshalb nicht automatisch unabhängige Bestätigungen. Mehr Indikatoren bedeuten nicht automatisch mehr Genauigkeit.

### Chartmuster
Chartmuster sind Preisformationen wie Kopf-Schulter-Formationen, Dreiecke und Keile. Sie können mögliche Fortsetzungen oder Umkehrungen beschreiben, sind aber interpretationsabhängig. Im Easy Engine werden nur Muster verwendet, deren Regeln maschinell klar, reproduzierbar und ausreichend testbar sind. Mehrdeutige oder fehleranfällige Formationen werden aus dem Easy-Pfad ausgeschlossen, nicht pauschal aus anderen Motoren.

### Vermögenspreis und fundamentaler Wert
- Vermögenspreis: der aktuell am Markt gehandelte Preis.
- Fundamentaler bzw. innerer Wert: eine Schätzung anhand wirtschaftlicher, finanzieller oder netzwerkspezifischer Eigenschaften.

Bei Kryptowerten ist ein klassischer innerer Wert häufig nicht direkt beobachtbar. Tokenpreis, Marktkapitalisierung, vollständig verwässerte Bewertung, Nutzungsdaten und Wertabschöpfung des Tokens sind unterschiedliche Größen und dürfen nicht gleichgesetzt werden. Die technische Analyse bewertet primär Preisverhalten; sie beweist nicht, dass ein Token fundamental unter- oder überbewertet ist.

## Grenzen der technischen Analyse

### 1. Marktbedingungen können sich abrupt ändern
Ein aus vergangenen Kurs- und Volumendaten abgeleiteter Trend kann durch Liquidationen, Liquiditätsengpässe, große Orders oder neue Informationen schnell kippen. Historische Muster garantieren keine Fortsetzung.

### 2. Rückwärtsgerichtete Daten
Technische Analyse verarbeitet vergangene und aktuelle Marktdaten. Zukünftige Ereignisse, regulatorische Entscheidungen, Sicherheitsvorfälle, Token-Unlocks oder unerwartete Nachrichten sind nicht zuverlässig aus einem Chart allein ableitbar. Solche Informationen gehören in separate News-, On-chain- und Fundamental-Module.

### 3. Interpretationsspielraum
Unterschiedliche Zeitfenster, Parameter und Regeln können aus denselben Daten verschiedene Ergebnisse liefern. Deshalb müssen Musterdefinitionen, Indikatorparameter, Timeframes und verwendete Kerzen reproduzierbar dokumentiert und getestet werden.

## Umsetzung im Easy Engine

- Schnell und deterministisch berechnen; redundante oder rechenintensive Bestätigungen vermeiden.
- Nur abgeschlossene Kerzen für bestätigte Signale verwenden; eine laufende Kerze darf höchstens als vorläufig gekennzeichnet werden.
- OHLCV-Reihen auf fehlende Werte, Duplikate, Zeitstempelreihenfolge und unplausible Preise prüfen.
- Bei unzureichender Historie den jeweiligen Indikator als UNKNOWN/UNAVAILABLE ausgeben, nicht als Nullwert oder erfundenen Wert.
- Indikatoren und Muster als Merkmale der Opportunity-Bewertung verwenden, nicht als neue universelle Pflicht-Gates.
- Bei widersprüchlichen Hinweisen Richtung, Konfidenz und Warnung sichtbar machen; nicht durch künstliche Gewissheit auflösen.
- Volatilität und ATR können Stop-Abstände unterstützen. Positionsgröße und Risiko sind separat zu berechnen und im Dashboard offenzulegen.
- Technische Analyse allein darf externe Ereignisse oder fundamentale Risiken nicht als geprüft darstellen.

## Erwartete Ausgabe

Für jede technische Bewertung sollen, soweit Daten verfügbar sind, mindestens folgende Informationen ausgegeben werden:
- Richtung: bullish, bearish, neutral oder unknown.
- Trendstatus und verwendeter Timeframe.
- Erkannte Indikatoren und Candlestick-/Chartmuster mit Parametern.
- Unterstützungs- und Widerstandszonen sowie Breakout-/False-Breakout-Hinweise.
- Datenstatus, Datenzeitpunkt und gegebenenfalls Warnungen.
- Opportunity-Score bzw. Beitrag zur Bewertung, getrennt von der Risikobewertung.
- Entry, Stop-Loss und Ziele nur dann, wenn sie aus gültigen Marktdaten nachvollziehbar berechnet werden können.

## Hinweis

Dieser Inhalt dient ausschließlich Bildungs- und Informationszwecken und ist keine Finanz-, Anlage- oder Handelsberatung. Technische Analyse ist keine exakte Wissenschaft. Handelsentscheidungen und deren Risiken liegen beim Nutzer.


## Multi-Timeframe-Analyse

### Unterstützte Zeitrahmen

Der Easy Engine unterstützt folgende Chart-Zeiträume:

| Timeframe | Dauer | Typischer Nutzen |
|---|---:|---|
| 1m | 1 Minute | sehr kurzfristige Bewegung und präzises Timing |
| 5m | 5 Minuten | kurzfristige Setups und Intraday-Einstiege |
| 15m | 15 Minuten | Intraday-Struktur, Momentum und lokale Zonen |
| 30m | 30 Minuten | breiterer Intraday-Kontext |
| 1h | 1 Stunde | kurzfristiger Trend und wichtige Preiszonen |
| 4h | 4 Stunden | übergeordneter Swing-Kontext |
| 1D | 1 Tag | täglicher Trend und größere Unterstützungs-/Widerstandszonen |
| 1W | 1 Woche | längerfristige Marktstruktur |
| 1M | 1 Monat | langfristiger Zyklus- und Kontextblick |

Ein 1Y-Chart kann optional als langfristige Visualisierung bereitgestellt werden; er ist kein zusätzlicher Pflicht-Timeframe für die Easy-Signalerzeugung.

### Anwendung nach Handelsstil

- Sehr kurzfristige Intraday-Setups: 1m und 5m für Timing; 15m und 1h als zusätzlicher Kontext.
- Intraday-Handel: 5m und 15m für Setup und Einstieg; 30m und 1h zur Einordnung.
- Swing-Trading: 15m/1h für Einstieg und 4h/1D für den übergeordneten Kontext.
- Längerfristige Positionen: 4h/1D für Struktur und 1W/1M für den langfristigen Kontext.

Dies sind Startprofile, keine starren Vorgaben. Der Nutzer soll den primären Analyse-Timeframe auswählen können.

### Regeln für die Zusammenführung

- Jeder Timeframe wird separat aus seinen eigenen OHLCV-Daten berechnet.
- Höhere Timeframes liefern Kontext; sie dürfen ein kurzfristiges Easy-Setup nicht automatisch blockieren.
- Abweichende Richtungen werden als Multi-Timeframe-Konflikt samt betroffenen Zeitrahmen angezeigt und können den Opportunity-Score beeinflussen.
- Ein fehlender höherer Timeframe wird als UNKNOWN/UNAVAILABLE markiert und nicht als bearish, bullish oder neutral erfunden.
- Keine Look-ahead-Bias: Für bestätigte Signale dürfen nur vollständig abgeschlossene Kerzen des jeweiligen Timeframes verwendet werden. Eine noch laufende Kerze darf nur ausdrücklich als vorläufig angezeigt werden.
- Zeitstempel, Börsen-/Datenquelle, Zeitzone und Candle-Close-Status müssen nachvollziehbar sein.
- Aggregierte Kerzen müssen lückenlos und korrekt aus den kleineren Kerzen gebildet werden; unvollständige Intervalle dürfen nicht als abgeschlossene Kerzen gelten.

## Erkennung von Aufwärts- und Abwärtstrends

### Aufwärtstrend (Bullish Structure)

Ein Aufwärtstrend wird durch eine Folge bestätigter höherer Hochs (Higher Highs, HH) und höherer Tiefs (Higher Lows, HL) beschrieben. Jeder bestätigte Swing-High liegt über dem vorherigen Swing-High und jedes bestätigte Swing-Low über dem vorherigen Swing-Low.

### Abwärtstrend (Bearish Structure)

Ein Abwärtstrend wird durch eine Folge bestätigter tieferer Hochs (Lower Highs, LH) und tieferer Tiefs (Lower Lows, LL) beschrieben. Jedes bestätigte Swing-High liegt unter dem vorherigen Swing-High und jedes bestätigte Swing-Low unter dem vorherigen Swing-Low.

### Seitwärtsmarkt und Strukturwechsel

- Seitwärts/Range: Es liegt keine konsistente Folge von HH/HL oder LH/LL vor; Hochs und Tiefs bewegen sich innerhalb einer erkennbaren Spanne.
- Möglicher Strukturwechsel: Ein relevantes vorheriges Swing-Level wird gebrochen, aber der Bruch ist noch nicht durch die festgelegten Regeln bestätigt.
- Bestätigter Strukturwechsel: Die deterministische Pivot-/Close-Regel des Motors bestätigt den Bruch. Die verwendete Regel muss im Ergebnis offengelegt werden.

### Unterstützung und Widerstand im Trend

- In einem Aufwärtstrend können frühere Swing-Highs nach einem bestätigten Ausbruch zu potenziellen Unterstützungszonen werden. Rücksetzer können höhere Tiefs bilden.
- In einem Abwärtstrend können frühere Swing-Lows nach einem bestätigten Bruch zu potenziellen Widerstandszonen werden. Erholungen können tiefere Hochs bilden.
- Zonen werden aus bestätigten Swing-Punkten und wiederholten Reaktionen abgeleitet. Sie sind Bereiche, keine garantierten exakten Linien.
- Die Steigung einer Trendlinie ist ergänzender Kontext; sie ersetzt nicht die Prüfung der Hoch-/Tief-Struktur.

### Maschinelle Erkennung und Ausgabe

Damit die Erkennung schnell und reproduzierbar bleibt, muss der Easy Engine:
- eine klar definierte Pivot-/Swing-High-/Swing-Low-Methode mit konfigurierbaren Parametern verwenden;
- nur bestätigte Swing-Punkte für bestätigte Trendlabels verwenden und die Bestätigungsverzögerung berücksichtigen;
- Mindestabstand/Preisbewegung oder eine andere dokumentierte Regel gegen Mikro-Pivots und Rauschen einsetzen;
- Trendlabels je Timeframe getrennt berechnen;
- Bullish, Bearish, Sideways, Transition und Unknown unterscheiden;
- HH, HL, LH und LL, gebrochene Levels, Zonen und letzte Bestätigungszeit ausgeben;
- Tests für Grenzfälle, fehlende Kerzen, doppelte Zeitstempel, unvollständige Kerzen und widersprüchliche Swings enthalten.

### Dashboard-Ausgabe

Die technische Übersicht soll je Asset mindestens zeigen:
- ausgewählten primären Timeframe;
- Trendstatus pro unterstütztem Timeframe;
- letzte bestätigte HH/HL- oder LH/LL-Struktur;
- relevante Unterstützungs- und Widerstandszonen;
- Konflikte zwischen kurzem und höherem Timeframe;
- Datenquelle, Zeitstempel und Status der letzten Kerze.

Die Multi-Timeframe-Analyse ist ein Kontext- und Bewertungsmerkmal. Sie führt keine neuen obligatorischen Easy-Gates ein.
