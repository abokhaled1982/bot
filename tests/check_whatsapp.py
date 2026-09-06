#!/usr/bin/env python3
"""
check_whatsapp.py — Preflight-Test fuer die WhatsApp-Bridge.

Was das Skript macht:
  1. GET  /health   → Bridge erreichbar? gepairt?
  2. GET  /chats    → Liste aller Chats/Gruppen/Kanaele mit ihrer ID
  3. POST /send     → optionale Test-Nachricht an --to oder $WHATSAPP_TO

Nichts davon fasst Binance an — reiner Kommunikations-Test.

Beispiel:
  python3 check_whatsapp.py                     # Health + Chat-Liste
  python3 check_whatsapp.py --send              # zusaetzlich Test-Msg an $WHATSAPP_TO
  python3 check_whatsapp.py --send --to 4917xxxxxxxx
  python3 check_whatsapp.py --send --to 12036xxxxxxxxx@newsletter   # Kanal
  python3 check_whatsapp.py --filter kanal      # Chats filtern (Name-Substring)
  python3 check_whatsapp.py --list-channels     # nur Newsletter/Kanaele auflisten
  python3 check_whatsapp.py --send --channel-name "Deals Boss"       # Kanal per Name
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

BRIDGE_URL           = os.getenv("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:3000")
BRIDGE_TOKEN         = os.getenv("WHATSAPP_BRIDGE_TOKEN", "")
DEFAULT_TO           = os.getenv("WHATSAPP_TO", "")
DEFAULT_CHANNEL_NAME = os.getenv("WHATSAPP_CHANNEL_NAME", "")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if BRIDGE_TOKEN:
        h["Authorization"] = f"Bearer {BRIDGE_TOKEN}"
    return h


def check_health() -> dict:
    print(f"\n▶ GET {BRIDGE_URL}/health")
    r = requests.get(f"{BRIDGE_URL}/health", headers=_headers(), timeout=5)
    r.raise_for_status()
    data = r.json()
    ok       = bool(data.get("ready"))
    awaiting = bool(data.get("awaitingQr"))
    print(f"  ready:        {ok}")
    print(f"  awaitingQr:   {awaiting}")
    print(f"  sent:         {data.get('sent', 0)}")
    print(f"  received:     {data.get('received', 0)}")
    print(f"  lastReadyAt:  {data.get('lastReadyAt', 0)}")
    if data.get("lastError"):
        print(f"  lastError:    {data['lastError']}")
    if awaiting:
        print("  ⚠️  Bridge wartet auf QR-Scan — in WhatsApp: "
              "Einstellungen → Verknuepfte Geraete → Geraet verknuepfen.")
    return data


def list_chats(name_filter: str = "") -> list[dict]:
    print(f"\n▶ GET {BRIDGE_URL}/chats"
          + (f"  (Filter: {name_filter!r})" if name_filter else ""))
    r = requests.get(f"{BRIDGE_URL}/chats", headers=_headers(), timeout=15)
    if r.status_code == 503:
        print("  Bridge nicht ready. Erst QR-Code scannen.")
        return []
    r.raise_for_status()
    data = r.json()
    chats: list[dict] = data.get("chats", [])
    if name_filter:
        f = name_filter.lower()
        chats = [c for c in chats if f in (c.get("name") or "").lower()
                                     or f in (c.get("id") or "").lower()]
    print(f"  Treffer: {len(chats)}")
    for c in chats[:60]:
        ts = c.get("lastTs") or 0
        ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds") \
                 if ts else "-"
        marker = {"group": "👥", "channel": "📣", "user": "👤"}.get(c.get("kind"), "?")
        print(f"  {marker} {c['kind']:8}  {c['id']:<35}  ({ts_iso})  {c.get('name','')}")
    if len(chats) > 60:
        print(f"  … {len(chats) - 60} weitere")
    return chats


def list_channels() -> list[dict]:
    print(f"\n▶ GET {BRIDGE_URL}/channels")
    r = requests.get(f"{BRIDGE_URL}/channels", headers=_headers(), timeout=15)
    if r.status_code == 503:
        print("  Bridge nicht ready.")
        return []
    if r.status_code == 404:
        print("  /channels nicht verfuegbar (alte Bridge). Nutze --filter stattdessen.")
        return []
    r.raise_for_status()
    chans: list[dict] = r.json().get("chats", [])
    print(f"  Treffer: {len(chans)}")
    for c in chans:
        print(f"  📣 {c['id']:<40}  {c.get('name','')}")
    return chans


def resolve_channel_id(name: str) -> str | None:
    """Kanal-Name → JID. Erst /channels, dann /find als Fallback."""
    needle = name.strip().lower()
    if not needle:
        return None

    try:
        r = requests.get(f"{BRIDGE_URL}/channels", headers=_headers(), timeout=15)
        if r.ok:
            for c in r.json().get("chats", []):
                if needle in (c.get("name") or "").lower():
                    return c.get("id")
    except requests.RequestException as e:
        print(f"  /channels Fehler: {e}")

    try:
        r = requests.get(f"{BRIDGE_URL}/find", headers=_headers(),
                         params={"name": name}, timeout=15)
        if r.ok:
            for c in r.json().get("chats", []):
                if c.get("isChannel") and needle in (c.get("name") or "").lower():
                    return c.get("id")
    except requests.RequestException as e:
        print(f"  /find Fehler: {e}")

    return None


def send_test(to: str, body: str) -> dict:
    print(f"\n▶ POST {BRIDGE_URL}/send  → {to}")
    r = requests.post(
        f"{BRIDGE_URL}/send",
        headers=_headers(),
        json={"to": to, "message": body},
        timeout=15,
    )
    print(f"  {r.status_code} {r.reason}")
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:200]}
    print(f"  {json.dumps(data, ensure_ascii=False)}")
    r.raise_for_status()
    return data


def main() -> int:
    p = argparse.ArgumentParser(description="Preflight fuer die WhatsApp-Bridge")
    p.add_argument("--send", action="store_true", help="Test-Nachricht schicken")
    p.add_argument("--to",   default=DEFAULT_TO,
                   help="Ziel (Rufnummer, xxxx@c.us, xxxx@g.us, xxxx@newsletter)")
    p.add_argument("--channel-name", default=DEFAULT_CHANNEL_NAME,
                   help="Kanal per Name aufloesen (z.B. 'Deals Boss'). "
                        "Ueberschreibt --to, wenn gesetzt.")
    p.add_argument("--message", default="🤖 Bridge-Test — wenn du das siehst, funktioniert der Kanal.")
    p.add_argument("--filter", default="", help="Chat-Liste nach Name/ID filtern")
    p.add_argument("--skip-chats", action="store_true", help="Chat-Liste ueberspringen")
    p.add_argument("--list-channels", action="store_true",
                   help="Nur Newsletter-Kanaele auflisten (via /channels)")
    args = p.parse_args()

    try:
        health = check_health()
    except Exception as e:
        print(f"\n❌ Health-Check fehlgeschlagen: {e}")
        print(f"   Laeuft die Bridge? → cd whatsapp_bridge && node server.js")
        return 2
    if not health.get("ready"):
        print("\n❌ Bridge ist noch nicht ready. Erst QR-Code scannen, dann nochmal.")
        return 3

    if not args.skip_chats:
        try:
            list_chats(args.filter)
        except Exception as e:
            print(f"⚠️ /chats Fehler: {e}")

    if args.list_channels:
        try:
            list_channels()
        except Exception as e:
            print(f"⚠️ /channels Fehler: {e}")

    if args.send:
        target = args.to
        if args.channel_name:
            print(f"\n▶ Aufloesen: Kanal-Name {args.channel_name!r}")
            resolved = resolve_channel_id(args.channel_name)
            if not resolved:
                print(f"❌ Kanal {args.channel_name!r} nicht gefunden. "
                      f"Ist er in WhatsApp abonniert?")
                return 6
            print(f"  → {resolved}")
            target = resolved

        if not target:
            print("\n⚠️ Keine Ziel-Adresse — setze WHATSAPP_TO, --to oder --channel-name.")
            return 4
        try:
            send_test(target, args.message)
            print("\n✅ Nachricht in die Queue der Bridge gegeben. Schau in WhatsApp.")
        except requests.HTTPError as e:
            print(f"\n❌ Send fehlgeschlagen: {e}")
            return 5

    return 0


if __name__ == "__main__":
    sys.exit(main())
