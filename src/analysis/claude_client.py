import aiohttp
import os
import json
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

class ClaudeAnalyzer:
    def __init__(self):
        self.base_url = os.getenv("OPENCLAW_BASE_URL", "http://localhost:18789/v1")
        self.api_key = os.getenv("OPENCLAW_API_KEY", "")
        # Analyse über Gemini Flash
        self.model_primary = os.getenv("LLM_MODEL", "google/gemini-3.1-flash-lite-preview")
        self.model_fast = "google/gemini-3.1-flash-lite-preview"
        
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        self.system_prompt = (
            "You are a Memecoin Analyst. Respond ONLY with valid JSON. Do not use Markdown blocks.\n"
            "Format:\n"
            "{\n"
            '  "hype_score": 0-100,\n'
            '  "risk_flags": ["Pump_Suspicion", "Rugpull_Hint", "None"],\n'
            '  "sentiment": "Bullish|Neutral|Bearish",\n'
            '  "key_signals": ["signal1", "signal2", "signal3"]\n'
            "}"
        )

    async def analyze_token(self, messages: list) -> dict:
        """
        Analyse mit Gemini Flash für den Bot.
        """
        
        compressed_msgs = self._compress_messages(messages)
        prompt = (
            "Analyze these recent messages for a memecoin.\n"
            f"Messages:\n{compressed_msgs}"
        )
        
        payload = {
            "model": self.model_primary,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 150
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions", headers=self.headers, json=payload) as response:
                    response_text = await response.text()
                    logger.info(f"DEBUG: Status {response.status}, Body: {response_text}")
                    if response.status == 200:
                        data = await response.json()
                        raw_content = data['choices'][0]['message']['content']
                        try:
                            clean_json = raw_content.replace('```json', '').replace('```', '').strip()
                            return json.loads(clean_json)
                        except json.JSONDecodeError:
                            logger.error(f"Gemini returned invalid JSON")
                            return None
                    return None
        except Exception as e:
            logger.info(f"Response: {response.status}, Data: {await response.text()}"); logger.error(f"Error calling Gemini via OpenClaw: {str(e)}")
            return None

    def _compress_messages(self, messages: list) -> str:
        sorted_msgs = sorted(messages, key=lambda x: x.get('weight', 0), reverse=True)
        top_msgs = sorted_msgs[:5]
        compressed = []
        for msg in top_msgs:
            source = msg.get('source', 'Unknown')
            weight = msg.get('weight', 'Low')
            content = msg.get('content', '')[:60]
            compressed.append(f"[{source}|{weight}] {content}")
        return "\n".join(compressed)
