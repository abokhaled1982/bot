import asyncio
import os
import sys
from loguru import logger
from dotenv import load_dotenv
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))
from analysis.claude_client import ClaudeAnalyzer

load_dotenv()

async def test_analysis():
    analyzer = ClaudeAnalyzer()
    # Simuliere eine typische Telegram-Nachricht
    sample_messages = [
        {"source": "Telegram-Tier1:12345", "content": "🚨 NEU: $SUGMI gerade gedroppt! Liquidity ist laut DexScreener locked. Dev hat 30% verbrannt. LFG! 🚀", "weight": 3.0}
    ]
    print("--- SENDE AN GEMINI ---")
    print(f"Eingabe: {sample_messages[0]['content']}")
    
    result = await analyzer.analyze_token(sample_messages)
    print("--- GEMINI ANTWORT ---")
    print(result)

if __name__ == "__main__":
    asyncio.run(test_analysis())
