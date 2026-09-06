/**
 * whatsapp_bridge/server.js
 *
 * Kleiner HTTP-Bruecken-Dienst zwischen dem Python-Bot und WhatsApp Web.
 *
 * Endpoints (localhost only):
 *   GET  /health          → status & QR-Auth-Status
 *   POST /send            → { to: "49123...", message: "..." }
 *
 * Inbound:
 *   Eingehende WhatsApp-Nachrichten werden per POST an WEBHOOK_URL geliefert
 *   (falls gesetzt), als { from, body, timestamp, chatName, isGroup }.
 *
 * Auth: Beim ersten Start wird ein QR-Code im Terminal gezeigt. Nach dem
 * Scannen wird die Session in ./.wwebjs_auth persistiert.
 *
 * Konfiguration ueber ENV:
 *   BRIDGE_PORT       (default 3000)
 *   ALLOWED_TOKEN     (optional Bearer-Token fuer /send und /health)
 *   WEBHOOK_URL       (optional, wo eingehende Msgs hin-POST-en)
 *   ONLY_FROM         (optional CSV: nur diese Chats an den Webhook reichen)
 */
'use strict';

const express = require('express');
const qrcode  = require('qrcode-terminal');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT           = parseInt(process.env.BRIDGE_PORT || '3000', 10);
const ALLOWED_TOKEN  = process.env.ALLOWED_TOKEN || '';
const WEBHOOK_URL    = process.env.WEBHOOK_URL || '';
const ONLY_FROM      = (process.env.ONLY_FROM || '').split(',').map(s => s.trim()).filter(Boolean);

const state = {
    ready:      false,
    qr:         null,          // aktueller QR-String (nur bis erfolgreiches Login)
    lastReadyAt: 0,
    sent:       0,
    received:   0,
    lastError:  null,
    restarts:   0,
    loggedOut:  false,
    recent:     [],            // {id, name, kind, ts, preview} — letzte eingehende Chats
};

const RECENT_MAX = 20;

function rememberInbound(msg, chatName) {
    const id = msg.from;
    if (!id) return;
    const kind = id.endsWith('@g.us') ? 'group'
              : id.endsWith('@newsletter') ? 'channel'
              : 'user';
    const entry = {
        id, kind, name: chatName || '',
        ts: msg.timestamp || Math.floor(Date.now() / 1000),
        preview: String(msg.body || '').slice(0, 60),
    };
    state.recent = [entry, ...state.recent.filter(x => x.id !== id)].slice(0, RECENT_MAX);
}

let client = null;
let restartTimer = null;

function buildClient() {
    return new Client({
        authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
        puppeteer: {
            headless: true,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu',
            ],
        },
    });
}

function attachHandlers(c) {
    c.on('qr', (qr) => {
        state.qr = qr;
        state.loggedOut = false;
        console.log('\n[BRIDGE] QR-Code — mit WhatsApp scannen:\n');
        qrcode.generate(qr, { small: true });
    });

    c.on('authenticated', () => {
        console.log('[BRIDGE] authenticated');
    });

    c.on('auth_failure', (msg) => {
        state.lastError = `auth_failure: ${msg}`;
        console.error('[BRIDGE]', state.lastError);
    });

    c.on('ready', () => {
        state.ready = true;
        state.qr = null;
        state.loggedOut = false;
        state.lastReadyAt = Date.now();
        console.log('[BRIDGE] ready ✔');
    });

    c.on('disconnected', (reason) => {
        state.ready = false;
        state.lastError = `disconnected: ${reason}`;
        console.warn('[BRIDGE]', state.lastError);
        if (String(reason).toUpperCase() === 'LOGOUT') {
            state.loggedOut = true;
            console.warn('[BRIDGE] Session vom WhatsApp-Server abgemeldet.');
            console.warn('[BRIDGE] Bitte .wwebjs_auth/ loeschen und neu koppeln:');
            console.warn('[BRIDGE]   rm -rf .wwebjs_auth .wwebjs_cache && node server.js');
        }
        scheduleRestart(5000);
    });

    c.on('message', async (msg) => {
        state.received += 1;
        if (msg.fromMe) return;
        if (ONLY_FROM.length && !ONLY_FROM.some(x => msg.from.startsWith(x))) return;

        const chatName = (await msg.getChat().catch(() => null))?.name || '';
        rememberInbound(msg, chatName);

        if (!WEBHOOK_URL) {
            console.log(`[BRIDGE] IN ${msg.from} (${chatName || '-'}): ${msg.body}`);
            return;
        }
        const payload = {
            from:      msg.from,
            body:      msg.body,
            timestamp: msg.timestamp,
            chatName,
            isGroup:   msg.from.endsWith('@g.us'),
        };
        try {
            const res = await fetch(WEBHOOK_URL, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(payload),
            });
            if (!res.ok) console.warn('[BRIDGE] webhook non-2xx:', res.status);
        } catch (e) {
            console.warn('[BRIDGE] webhook error:', e.message);
        }
    });
}

function initializeClient() {
    client = buildClient();
    attachHandlers(client);
    client.initialize().catch((e) => {
        state.lastError = `initialize: ${e.message}`;
        console.error('[BRIDGE] initialize failed:', e.message);
        scheduleRestart(10_000);
    });
}

function scheduleRestart(delayMs) {
    if (state.loggedOut) return; // ohne frische .wwebjs_auth waere Restart sinnlos
    if (restartTimer) return;
    state.restarts += 1;
    console.warn(`[BRIDGE] restart in ${Math.round(delayMs / 1000)}s …`);
    restartTimer = setTimeout(async () => {
        restartTimer = null;
        try { await client?.destroy(); } catch { /* ignore */ }
        initializeClient();
    }, delayMs);
}

process.on('unhandledRejection', (reason) => {
    const msg = reason && reason.message ? reason.message : String(reason);
    state.lastError = `unhandledRejection: ${msg}`;
    console.warn('[BRIDGE] unhandledRejection:', msg);
    if (/execution context was destroyed|Target closed|Protocol error/i.test(msg)) {
        state.ready = false;
        scheduleRestart(3000);
    }
});

process.on('uncaughtException', (err) => {
    state.lastError = `uncaughtException: ${err.message}`;
    console.error('[BRIDGE] uncaughtException:', err.message);
    state.ready = false;
    scheduleRestart(3000);
});

initializeClient();

// ── HTTP-API ──────────────────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '128kb' }));

app.use((req, res, next) => {
    if (!ALLOWED_TOKEN) return next();
    const got = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
    if (got === ALLOWED_TOKEN) return next();
    return res.status(401).json({ ok: false, error: 'unauthorized' });
});

app.get('/health', (_req, res) => {
    res.json({
        ok:           true,
        ready:        state.ready,
        awaitingQr:   !!state.qr,
        lastReadyAt:  state.lastReadyAt,
        sent:         state.sent,
        received:     state.received,
        lastError:    state.lastError,
    });
});

// Aktuellen QR-String zurueckliefern (nur solange noch nicht gepairt).
app.get('/qr', (_req, res) => {
    if (state.ready) return res.json({ ok: true, ready: true, qr: null });
    if (!state.qr)   return res.status(503).json({ ok: false, error: 'no qr yet' });
    res.json({ ok: true, ready: false, qr: state.qr });
});

// Liste aller Chats (Kontakte, Gruppen, Kanaele). Wenn getChats crasht
// (bekannter WA-Web-Bug), liefern wir die zuletzt eingegangenen zurueck.
app.get('/chats', async (_req, res) => {
    if (!state.ready) return res.status(503).json({ ok: false, error: 'not ready' });
    try {
        const chats = await client.getChats();
        const out = chats.map(c => {
            const id = c.id?._serialized || '';
            let kind = 'user';
            if (c.isGroup)                   kind = 'group';
            else if (id.endsWith('@newsletter')) kind = 'channel';
            return {
                id,
                name:       c.name || c.formattedTitle || '',
                kind,
                unread:     c.unreadCount || 0,
                lastTs:     c.timestamp || 0,
            };
        }).sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
        res.json({ ok: true, count: out.length, chats: out });
    } catch (e) {
        console.error('[BRIDGE] getChats failed, returning recent inbound instead:', e.message);
        res.status(200).json({
            ok: true,
            fallback: 'recent-inbound',
            hint: 'getChats() nicht verfuegbar — schreibe in den Ziel-Chat, dann taucht er hier auf.',
            count: state.recent.length,
            chats: state.recent.map(r => ({
                id: r.id, name: r.name, kind: r.kind, unread: 0, lastTs: r.ts,
            })),
        });
    }
});

// Zuletzt eingegangene Chat-IDs — funktioniert auch wenn getChats bricht.
app.get('/recent', (_req, res) => {
    res.json({ ok: true, count: state.recent.length, chats: state.recent });
});

// Newsletter-Kanaele (z.B. "Deals Boss") ueber die dedizierte API auflisten.
// getChats() liefert Kanaele in vielen wwebjs-Versionen nicht mit; getChannels()
// kracht in aelteren Versionen (~1.24). Deshalb: erst API, dann Store-Fallback.
app.get('/channels', async (_req, res) => {
    if (!state.ready) return res.status(503).json({ ok: false, error: 'not ready' });

    if (typeof client.getChannels === 'function') {
        try {
            const channels = await client.getChannels();
            const out = channels.map(c => ({
                id:     c.id?._serialized || '',
                name:   c.name || c.formattedTitle || c.subject || '',
                kind:   'channel',
                unread: c.unreadCount || 0,
                lastTs: c.timestamp || 0,
            })).filter(c => c.id).sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
            return res.json({ ok: true, source: 'getChannels', count: out.length, chats: out });
        } catch (e) {
            console.warn('[BRIDGE] getChannels() failed, using store fallback:', e.message);
        }
    }

    try {
        const out = await client.pupPage.evaluate(() => {
            const seen = new Map();
            const pushModel = (m) => {
                if (!m) return;
                const id = (m.id && m.id._serialized) || m.id || '';
                if (!id || typeof id !== 'string' || !id.endsWith('@newsletter')) return;
                if (seen.has(id)) return;
                seen.set(id, {
                    id,
                    name:   m.name || m.formattedTitle || m.subject || m.displayName || '',
                    kind:   'channel',
                    unread: m.unreadCount || 0,
                    lastTs: m.t || m.timestamp || 0,
                });
            };
            const collect = (coll) => {
                if (!coll) return;
                const arr = (typeof coll.getModelsArray === 'function' && coll.getModelsArray())
                         || coll.models || (Array.isArray(coll) ? coll : []);
                for (const m of arr) pushModel(m);
            };
            const S = window.Store || {};
            collect(S.NewsletterCollection);
            collect(S.Newsletter);
            collect(S.Channel);
            collect(S.ChannelCollection);
            if (S.Chat) {
                const chats = (typeof S.Chat.getModelsArray === 'function' && S.Chat.getModelsArray()) || [];
                for (const c of chats) {
                    const id = (c.id && c.id._serialized) || '';
                    if (id.endsWith('@newsletter')) pushModel(c);
                }
            }
            return Array.from(seen.values()).sort((a, b) => (b.lastTs || 0) - (a.lastTs || 0));
        });
        return res.json({ ok: true, source: 'store', count: out.length, chats: out });
    } catch (e) {
        console.error('[BRIDGE] /channels store fallback failed:', e.message);
        return res.status(500).json({ ok: false, error: e.message });
    }
});

// Chat nach Name suchen — greift direkt auf window.Store.Chat zu und umgeht
// damit den bekannten getChats()-Bug in whatsapp-web.js.
//   GET /find?name=deals
app.get('/find', async (req, res) => {
    if (!state.ready) return res.status(503).json({ ok: false, error: 'not ready' });
    const q = String(req.query.name || '').toLowerCase().trim();
    if (!q) return res.status(400).json({ ok: false, error: 'missing name query param' });
    try {
        const list = await client.pupPage.evaluate((needle) => {
            const store = window.Store && window.Store.Chat;
            const arr = (store && store.getModelsArray && store.getModelsArray()) || [];
            return arr.map((c) => {
                const id      = (c.id && c.id._serialized) || '';
                const name    = c.name || c.formattedTitle || c.subject || '';
                const isGroup = !!c.isGroup;
                const isChan  = !!c.isNewsletter || !!c.isChannel || id.endsWith('@newsletter');
                return { id, name, isGroup, isChannel: isChan };
            }).filter((c) => c.id && (
                c.name.toLowerCase().includes(needle) ||
                c.id.toLowerCase().includes(needle)
            ));
        }, q);
        res.json({ ok: true, count: list.length, chats: list });
    } catch (e) {
        console.error('[BRIDGE] /find failed:', e.message);
        res.status(500).json({ ok: false, error: e.message });
    }
});

function normalizeChatId(to) {
    if (!to) return null;
    if (to.includes('@')) return to;                           // schon `xxxx@c.us` oder `@g.us`
    const digits = String(to).replace(/[^0-9]/g, '');
    if (!digits) return null;
    return `${digits}@c.us`;
}

app.post('/send', async (req, res) => {
    if (!state.ready) {
        return res.status(503).json({ ok: false, error: 'not ready' });
    }
    const { to, message } = req.body || {};
    const chatId = normalizeChatId(to);
    if (!chatId || !message) {
        return res.status(400).json({ ok: false, error: 'missing to/message' });
    }
    try {
        const sent = await client.sendMessage(chatId, String(message));
        state.sent += 1;
        return res.json({ ok: true, id: sent.id?._serialized || null });
    } catch (e) {
        state.lastError = `send: ${e.message}`;
        console.error('[BRIDGE] send error:', e);
        return res.status(500).json({ ok: false, error: e.message });
    }
});

app.listen(PORT, '127.0.0.1', () => {
    console.log(`[BRIDGE] HTTP listening on 127.0.0.1:${PORT}` +
        (WEBHOOK_URL ? ` → webhook ${WEBHOOK_URL}` : ' (no webhook set)') +
        (ALLOWED_TOKEN ? ' (token auth)' : ' (no auth)'));
});

process.on('SIGINT',  () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
