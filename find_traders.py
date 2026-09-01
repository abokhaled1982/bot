#!/usr/bin/env python3
"""
find_traders.py — Finde profitable Scalper auf Hyperliquid

Nutzung:
    python3 find_traders.py              # Top-Trader der letzten 30 Tage
    python3 find_traders.py week         # Top-Trader der letzten 7 Tage
    python3 find_traders.py 0xABC...     # Analysiere einen bestimmten Trader

Keine API-Keys noetig. Komplett kostenlos.
"""
import json
import sys
import requests

HL_API = "https://api.hyperliquid.xyz/info"


def fetch_trader_positions(wallet: str) -> None:
    """Zeige alle offenen Positionen eines Traders."""
    print(f"\n{'=' * 60}")
    print(f"Trader: {wallet}")
    print(f"{'=' * 60}")

    # Account-Info
    resp = requests.post(HL_API, json={
        "type": "clearinghouseState", "user": wallet,
    }, timeout=15)
    data = resp.json()

    ms = data.get("marginSummary", {})
    print(f"\n💰 Account Value:  ${float(ms.get('accountValue', 0)):>14,.2f}")
    print(f"   Margin Used:    ${float(ms.get('totalMarginUsed', 0)):>14,.2f}")
    print(f"   Free Margin:    ${float(ms.get('totalRawUsd', 0)):>14,.2f}")

    positions = data.get("assetPositions", [])
    open_pos = [p for p in positions
                if abs(float(p["position"].get("sizeDecimal",
                       p["position"].get("size", "0")) or "0")) > 1e-12]

    if not open_pos:
        print("\n📭 Keine offenen Positionen.")
    else:
        print(f"\n📊 {len(open_pos)} offene Position(en):\n")
        print(f"  {'Coin':<8} {'Richtung':<8} {'Groesse $':<14} "
              f"{'Entry':<14} {'Hebel':<8} {'PnL $':<14}")
        print(f"  {'-'*8} {'-'*8} {'-'*14} {'-'*14} {'-'*8} {'-'*14}")

        for ap in open_pos:
            pos = ap["position"]
            coin = pos.get("coin", "?")
            size = float(pos.get("sizeDecimal",
                         pos.get("size", "0")) or "0")
            entry = float(pos.get("entryPx", "0") or "0")
            value = float(pos.get("positionValue", "0") or "0")
            pnl = float(pos.get("unrealizedPnl", "0") or "0")
            lev = pos.get("leverage", {})
            lev_val = float(
                lev.get("value", 1) if isinstance(lev, dict)
                else (lev or 1)
            )
            direction = "LONG" if size > 0 else "SHORT"

            print(f"  {coin:<8} {direction:<8} ${value:>12,.2f} "
                  f"${entry:>12,.2f} {lev_val:>6.0f}x "
                  f"${pnl:>+12,.2f}")

    # Letzte Trades
    resp2 = requests.post(HL_API, json={
        "type": "userFills", "user": wallet,
    }, timeout=15)
    fills = resp2.json()

    if isinstance(fills, list) and fills:
        print(f"\n📋 Letzte {min(10, len(fills))} Trades "
              f"(von {len(fills)} total):\n")
        print(f"  {'Coin':<8} {'Seite':<6} {'Groesse $':<12} "
              f"{'Preis':<14} {'Zeit'}")
        print(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*14} {'-'*20}")

        for fill in fills[:10]:
            coin = fill.get("coin", "?")
            side = fill.get("side", "?")
            px = float(fill.get("px", "0"))
            sz = float(fill.get("sz", "0"))
            value = px * sz
            ts = fill.get("time", "")
            if isinstance(ts, int):
                from datetime import datetime
                ts = datetime.fromtimestamp(ts / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            print(f"  {coin:<8} {side:<6} ${value:>10,.2f} "
                  f"${px:>12,.2f}  {ts}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "month"

    # Wenn eine Wallet-Adresse angegeben wurde
    if arg.startswith("0x"):
        fetch_trader_positions(arg)
        return

    # Sonst: Leaderboard abfragen
    window = arg
    print(f"\n🏆 Hyperliquid Leaderboard — Top Trader ({window})")
    print(f"{'=' * 60}")
    print("\nLade Daten von api.hyperliquid.xyz ...")

    resp = requests.post(HL_API, json={
        "type": "leaderboard", "window": window,
    }, timeout=15)

    if resp.status_code != 200:
        print(f"❌ API Fehler: {resp.status_code}")
        print(f"   Response: {resp.text[:200]}")
        return

    data = resp.json()

    # Die API gibt verschiedene Formate zurueck
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = (data.get("leaderboardRows")
                   or data.get("rows")
                   or [])
    else:
        print(f"Unbekanntes Format: {type(data)}")
        print(json.dumps(data, indent=2)[:500])
        return

    if not entries:
        print("Keine Eintraege gefunden.")
        print(f"Raw response: {json.dumps(data, indent=2)[:500]}")
        return

    print(f"\n{'Rang':<6} {'Wallet':<18} {'PnL':<16} {'Name'}")
    print(f"{'-'*6} {'-'*18} {'-'*16} {'-'*20}")

    for i, entry in enumerate(entries[:30], 1):
        wallet = entry.get("ethAddress", entry.get("trader", "?"))
        pnl = float(entry.get("accountValue",
                    entry.get("pnl", 0)))
        name = entry.get("displayName", "")
        short = (f"{wallet[:6]}...{wallet[-4:]}"
                 if len(wallet) > 12 else wallet)
        print(f"#{i:<5} {short:<18} ${pnl:>+14,.0f} {name}")

    print(f"\n💡 Tipp: Um einen Trader zu analysieren:")
    print(f"   python3 find_traders.py 0xWALLET_ADRESSE")
    print(f"\n💡 Tipp: Wallet in .env eintragen:")
    print(f"   HL_TRADER_WALLETS=0xADRESSE1,0xADRESSE2")


if __name__ == "__main__":
    main()
