# HARDWARE_SETUP.md – Hardware-Setup

Anleitung für die physische Installation: Relay-Board, GPIO-Verkabelung, Magnetventile.

---

## 1. Empfohlene Hardware

| Komponente | Empfehlung | Hinweis |
|---|---|---|
| Einplatinencomputer | <!-- TODO: Raspberry Pi-Modell eintragen --> | |
| Betriebssystem | <!-- TODO: OS-Version eintragen --> | |
| Relay-Board | <!-- TODO: Relay-Board-Modell und Bezugsquelle eintragen --> | 8-Kanal empfohlen für ≥ 6 Zonen |
| Netzteil Raspberry Pi | <!-- TODO: Spezifikation eintragen (z.B. 5V/3A USB-C) --> | Stabiles Netzteil wichtig |
| Netzteil Relay-Board | <!-- TODO: Spezifikation eintragen (z.B. 5V/2A separat) --> | Separat vom Pi wenn möglich |
| Magnetventile | <!-- TODO: Typ und Betriebsspannung eintragen (z.B. 24V AC oder 12V DC) --> | |
| Trafo/Netzteil Ventile | <!-- TODO: Spezifikation eintragen --> | |

---

## 2. GPIO-Pinbelegung (BCM-Nummerierung)

Der Raspberry Pi verwendet BCM-Nummerierung (Broadcom-Chip-Nummerierung). **Nicht** die physische Board-Nummerierung verwenden.

### Raspberry Pi GPIO-Pinout (Kurzreferenz)

```
                    3.3V  [ 1] [ 2]  5V
              SDA1 / GPIO2 [ 3] [ 4]  5V
              SCL1 / GPIO3 [ 5] [ 6]  GND
                    GPIO4  [ 7] [ 8]  GPIO14 / TXD0
                      GND  [ 9] [10]  GPIO15 / RXD0
                   GPIO17  [11] [12]  GPIO18
                   GPIO27  [13] [14]  GND
                   GPIO22  [15] [16]  GPIO23
                    3.3V  [17] [18]  GPIO24
              MOSI / GPIO10 [19] [20]  GND
              MISO / GPIO9  [21] [22]  GPIO25
              SCLK / GPIO11 [23] [24]  GPIO8  / CE0
                      GND  [25] [26]  GPIO7  / CE1
               ID_SD / GPIO0 [27] [28]  GPIO1  / ID_SC
                    GPIO5  [29] [30]  GND
                    GPIO6  [31] [32]  GPIO12
                   GPIO13  [33] [34]  GND
                   GPIO19  [35] [36]  GPIO16
                   GPIO26  [37] [38]  GPIO20
                      GND  [39] [40]  GPIO21
```

### Gültige BCM-Pins für GPIO-Steuerung

Gültige Pins: **2..27** (Pins 0 und 1 sind für I²C ID EEPROM reserviert).

<!-- TODO: Tatsächliches Pin-Mapping für das eingesetzte Relay-Board eintragen und prüfen -->

Empfohlene Pinbelegung (6 Zonen):

| Zone | BCM-Pin | Physischer Board-Pin |
|---|---|---|
| 1 | 17 | 11 |
| 2 | 18 | 12 |
| 3 | 27 | 13 |
| 4 | 22 | 15 |
| 5 | 23 | 16 |
| 6 | 24 | 18 |

---

## 3. Active-Low vs. Active-High: Elektrischer Hintergrund

### Active-Low (Standard, `IRRIGATION_RELAY_ACTIVE_LOW: true`)

```
GPIO HIGH (3.3V) → Optokoppler aus → Relais fällt ab → Ventil GESCHLOSSEN
GPIO LOW  (0V)   → Optokoppler an  → Relais zieht an → Ventil GEÖFFNET
```

**Vorteil bei Active-Low:**  
Beim GPIO-Init (vor der Software-Konfiguration) liegen alle Pins standardmäßig auf HIGH (Pullup). Das bedeutet: Alle Relais fallen ab → Alle Ventile bleiben **geschlossen**. Sicherheitsrelevant: kein ungewolltes Öffnen beim Systemstart.

**Typische Boards:** Die meisten chinesischen 4/8-Kanal-Relay-Boards mit Optokoppler-Trennung verwenden Active-Low-Logik.

### Active-High (`IRRIGATION_RELAY_ACTIVE_LOW: false`)

```
GPIO LOW  (0V)   → Relais fällt ab → Ventil GESCHLOSSEN
GPIO HIGH (3.3V) → Relais zieht an → Ventil GEÖFFNET
```

Für Boards ohne Optokoppler oder mit invertierter Logik.

### Bestimmung der Logik

Im Zweifelsfall: Relay-Board-Datenblatt lesen. Alternativ:
1. Backend mit `driver=sim` starten (kein GPIO-Zugriff)
2. Ein Ventil kurz manuell starten (`POST /start` mit sehr kurzer Laufzeit)
3. Relay-Board-LEDs beobachten: leuchten sie beim Starten auf?
4. Wenn ja und Ventil öffnet: Active-Low korrekt. Wenn LED leuchtet aber kein Wasser: Active-High versuchen.

---

## 4. Verkabelung: Relay-Board → Raspberry Pi

```
Raspberry Pi          Relay-Board
────────────────     ─────────────────────
Pin 2  (5V)    ──── VCC (Board-Stromversorgung, wenn 5V-Board)
Pin 6  (GND)   ──── GND
Pin 11 (GPIO17) ──── IN1  (Zone 1)
Pin 12 (GPIO18) ──── IN2  (Zone 2)
Pin 13 (GPIO27) ──── IN3  (Zone 3)
Pin 15 (GPIO22) ──── IN4  (Zone 4)
Pin 16 (GPIO23) ──── IN5  (Zone 5)
Pin 18 (GPIO24) ──── IN6  (Zone 6)
```

<!-- TODO: Tatsächliche Verkabelung für das eingesetzte Board prüfen und ggf. anpassen -->

**Wichtig:** Manche Relay-Boards benötigen eine externe 5V-Stromversorgung (nicht vom Pi) wenn mehrere Relais gleichzeitig schalten. Der Pi liefert maximal ~50mA auf den 5V-Pins – bei 8 Relais kann das zu Spannungseinbrüchen führen.

---

## 5. Verkabelung: Relay-Board → Magnetventile

```
Relay-Board COM  ──── Stromquelle (+) (24V AC Trafo oder 12V DC Netzteil)
Relay-Board NO   ──── Magnetventil-Klemme A
Magnetventil-Klemme B  ──── Stromquelle (-) / GND / Nullleiter

Oder:

Relay-Board COM  ──── Magnetventil-Klemme A
Relay-Board NO   ──── Stromquelle (+)
Magnetventil-Klemme B  ──── Stromquelle (-) / GND
```

<!-- TODO: Tatsächliche Betriebsspannung der Magnetventile und Trafo-Spezifikation eintragen -->

**NO vs. NC am Relay-Board:**
- **NO** (Normally Open) = Kontakt ist offen wenn Relais nicht anzieht → Ventil geschlossen im Ruhezustand ✓
- **NC** (Normally Closed) = Kontakt ist geschlossen wenn Relais nicht anzieht → Ventil offen im Ruhezustand ✗

Immer **NO** verwenden! Damit ist sichergestellt, dass alle Ventile bei Stromausfall des Relay-Boards geschlossen bleiben.

---

## 6. Sicherungsmaßnahmen

### 6.1 Freilaufdioden (Flyback-Dioden)

Magnetventile sind induktive Lasten. Beim Abschalten entsteht eine Spannungsspitze (Back-EMF), die Relais und Transistoren beschädigen kann.

Lösung: Freilaufdiode (z.B. 1N4007) parallel zum Magnetventil, in Sperrrichtung zur Betriebsspannung.

```
     +24V ─────┬────── Relais NO ─────── Ventil (+)
               │                              │
               │                         [1N4007]  ← Kathode zu +
               │                              │
     GND  ─────┴──────────────────────── Ventil (-)
```

Hochwertige Relay-Boards haben diese Dioden bereits integriert (im Schaltplan prüfen).

### 6.2 Optokoppler-Trennung

Relay-Boards mit Optokoppler-Trennung zwischen Steuerseite (GPIO/3.3V) und Lastseite (230V AC oder 24V AC) sind zwingend empfohlen. Ohne galvanische Trennung besteht das Risiko, den Raspberry Pi bei einem Kurzschluss auf der Lastseite zu zerstören.

### 6.3 Absicherung der Lastseite

<!-- TODO: Sicherungskonzept für die Ventilstromversorgung eintragen (z.B. 2A-Feinsicherung pro Stromkreis) -->

---

## 7. Schritt-für-Schritt: device_config.json für echtes Setup

### Schritt 1: Ventile zählen und Zonen nummerieren

```
Zone 1 = Bereich A (Gurken)
Zone 2 = Bereich B (Tomaten)
Zone 3 = Bereich C (Salat)
...
```

### Schritt 2: GPIO-Pins zuweisen

Freie GPIO-Pins auswählen (2..27, nicht für andere Funktionen verwendet).

### Schritt 3: Relay-Board anschließen

Gemäß Abschnitt 4 verkabeln.

### Schritt 4: device_config.json erstellen

```json
{
  "version": 1,
  "device": {
    "MAX_VALVES": 6,
    "IRRIGATION_VALVE_DRIVER": "rpi",
    "IRRIGATION_RELAY_ACTIVE_LOW": true,
    "IRRIGATION_GPIO_PINS": {
      "1": 17,
      "2": 18,
      "3": 27,
      "4": 22,
      "5": 23,
      "6": 24
    }
  },
  "hard_limits": {
    "MAX_RUNTIME_S": 3600,
    "MAX_CONCURRENT_VALVES": 1
  }
}
```

### Schritt 5: Funktionstest (ein Ventil, kurze Zeit)

```bash
# Backend starten
sudo systemctl start irrigation-backend

# Ventil 1 für 5 Sekunden öffnen
curl -X POST http://localhost:8000/start \
  -H "X-API-Key: $(sudo cat /opt/bewaesserung/data/api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"zone": 1, "duration": 5, "time_unit": "Sekunden"}'

# Prüfen ob Relay-LED leuchtet und Wasser fließt
# Nach 5 Sekunden: automatisch geschlossen
```

Jeden Kanal einzeln testen, bevor der Vollbetrieb aufgenommen wird.

### Schritt 6: Log prüfen

```bash
# Alle valve_start und valve_timeout Events
jq 'select(.event == "valve_start" or .event == "valve_timeout")' logs/irrigation.jsonl | tail -20
```

Erwartete Ausgabe: `valve_start` mit `"driver": "rpi"`, danach `valve_timeout` nach 5 Sekunden.
