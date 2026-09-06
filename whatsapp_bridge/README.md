# WhatsApp Bridge

Kleiner Node-Dienst, der WhatsApp-Web-Sessions haelt und den Python-Bot per
HTTP anspricht. Der Bot selbst kennt WhatsApp nicht — er redet nur mit dieser
Bruecke.

## Einmalig einrichten

```bash
cd whatsapp_bridge
npm install
```

`whatsapp-web.js` startet intern Chromium (Puppeteer). Auf Servern ohne
Display braucht das folgende Bibliotheken (Debian/Ubuntu):

```bash
sudo apt-get install -y libnss3 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxrandr2 libgbm1 libpangocairo-1.0-0 libgtk-3-0
```

## Erststart und WhatsApp koppeln

```bash
BRIDGE_PORT=3000 \
ALLOWED_TOKEN="$(openssl rand -hex 24)" \
WEBHOOK_URL="" \
node server.js
```

Ein QR-Code erscheint im Terminal. In der WhatsApp-App unter
**Einstellungen → Verknuepfte Geraete → Geraet verknuepfen** scannen.
Die Session wird in `.wwebjs_auth/` gespeichert und ueberlebt Neustarts.

## Environment fuer den Python-Bot

```bash
export WHATSAPP_BRIDGE_URL="http://127.0.0.1:3000"
export WHATSAPP_BRIDGE_TOKEN="…derselbe Token wie oben…"
export WHATSAPP_TO="49123456789"   # Telefonnr. mit Laendercode, ohne + oder Chat-ID `xxxxx@c.us`
```

## Endpoints

| Endpoint | Body | Zweck |
|---|---|---|
| `GET /health` | – | `{ ready, awaitingQr, sent, received, lastError }` |
| `POST /send` | `{ to, message }` | Nachricht an WhatsApp senden |

Beide Endpoints binden nur auf `127.0.0.1`. Wer `ALLOWED_TOKEN` setzt, muss
`Authorization: Bearer <token>` mitschicken.

## Betrieb dauerhaft laufen lassen

Fuer Produktion `pm2` oder `systemd`. Beispiel `pm2`:

```bash
sudo npm install -g pm2
pm2 start server.js --name whatsapp-bridge --env production
pm2 save
pm2 startup
```

## Sicherheit

* Der Node-Dienst hat vollen Sende-Zugriff auf dein WhatsApp-Konto. Nur auf
  vertrauenswuerdigen Hosts betreiben.
* `.wwebjs_auth/` niemals ins Repo committen (`.gitignore` deckt das ab).
* Bei Verdacht auf Kompromittierung: WhatsApp → verknuepftes Geraet abmelden,
  Ordner `.wwebjs_auth/` loeschen, Bridge neu koppeln.
