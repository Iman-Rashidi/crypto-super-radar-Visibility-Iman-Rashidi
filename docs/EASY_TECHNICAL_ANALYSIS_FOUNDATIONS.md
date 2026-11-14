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
