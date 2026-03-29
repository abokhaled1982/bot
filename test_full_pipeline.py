import asyncio
import os
import sys
from loguru import logger

# src Ordner zu Pfad hinzufügen
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from adapters.dexscreener import DexScreenerAdapter
from adapters.telegram_mirror import TelegramAlphaMirror
from analysis.claude_client import ClaudeAnalyzer
from analysis.fusion import SignalFusion

async def test_bot_pipeline():
    print("--- STARTE TEST-PIPELINE ---")
    
    dex = DexScreenerAdapter()
    tg = TelegramAlphaMirror()
    analyzer = ClaudeAnalyzer()
    fusion = SignalFusion()
    
    # 1. Token von DexScreener holen
    print("[1] Hole Daten von DexScreener...")
    boosted = await dex.get_boosted_tokens()
    if not boosted:
        print("Keine Token gefunden!")
        return
    token = boosted[0]
    address = token.get("address")
    symbol = token.get("symbol") or "UNKNOWN"
    print(f"-> Teste Token: {symbol} ({address})")
    
    token_data = await dex.get_token_data(address)
    print(f"-> DexScreener Daten: {token_data}")
    
    # 2. Telegram Mentions simulieren/abrufen
    print("[2] Suche nach Telegram Mentions...")
    messages = tg.get_recent_mentions(symbol, address, minutes=30)
    print(f"-> Telegram Mentions gefunden: {len(messages)}")
    
    # 3. LLM Analyse (ClaudeAnalyzer)
    print("[3] Sende an Gemini Flash (Claude Client)...")
    if not messages:
        claude_result = {"hype_score": 20, "risk_flags": ["No_Telegram_Data"], "sentiment": "Neutral", "key_signals": ["No_recent_mentions"]}
    else:
        claude_result = await analyzer.analyze_token(messages)
    print(f"-> LLM Ergebnis: {claude_result}")
    
    # 4. Fusion Score Berechnung
    print("[4] Berechne Fusion Score...")
    chain_data = {"liquidity_locked": True, "top_10_holder_percent": 30}
    market_data = {"btc_1h_change": 0.5}
    fusion_result = fusion.calculate_score(claude_result, chain_data, token_data, market_data)
    if not fusion_result:
        fusion_result = {"decision": "SKIP (Score Error)", "score": 0.0}
    print(f"-> Fusion Ergebnis: {fusion_result}")
    
    # WhatsApp Update testen
    from notify_whatsapp import send_whatsapp_update
    final_msg = (
        f"🚀 *TEST: {symbol} ANALYSIERT*\n"
        f"Status: {fusion_result.get('decision', 'N/A')} (Score: {fusion_result.get('score', 0)})\n"
        f"Sentiment: {claude_result.get('sentiment', 'N/A')}"
    )
    send_whatsapp_update(final_msg)
    print("-> WhatsApp Update gesendet.")
    
    print("--- TEST-PIPELINE ABGESCHLOSSEN ---")

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(test_bot_pipeline())
