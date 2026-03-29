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
        # Updated to Gemini Flash
        self.model_primary = os.getenv("LLM_MODEL", "amazon-bedrock/eu.anthropic.claude-sonnet-4-6")
        self.model_fast = "amazon-bedrock/us.anthropic.claude-3-5-haiku-20241022-v1:0" # Fast model for Strategy 5
        
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        # System prompt remains for JSON compliance
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

    async def analyze_token(self, messages: list, use_haiku: bool = True) -> dict:
        """
        Strategy 5: Model Hierarchy.
        Start with Haiku. If Haiku score > 65, we could potentially call Sonnet.
        Currently, this function executes the call and returns the parsed JSON.
        """
        
        # Strategy 3: Compress messages
        compressed_msgs = self._compress_messages(messages)
        
        prompt = (
            "Analyze these recent messages for a memecoin.\n"
            f"Messages:\n{compressed_msgs}"
        )

        model = self.model_fast if use_haiku else self.model_primary
        
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,  # Deterministic
            "max_tokens": 150    # Strategy 6: cap tokens for cost saving
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.base_url}/chat/completions", headers=self.headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        raw_content = data['choices'][0]['message']['content']
                        
                        # Usage tracking for token budget
                        usage = data.get('usage', {})
                        await self._log_usage(model, usage)
                        
                        # Parse JSON
                        try:
                            # Strip potential markdown if Claude disobeys
                            clean_json = raw_content.replace('```json', '').replace('```', '').strip()
                            result = json.loads(clean_json)
                            return result
                        except json.JSONDecodeError:
                            logger.error(f"Claude returned invalid JSON: {raw_content}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"OpenClaw API error: {response.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"Error calling Claude via OpenClaw: {str(e)}")
            return None

    def _compress_messages(self, messages: list) -> str:
        """
        Strategy 3: Compress messages to save tokens.
        Format: [Source|Weight] Short content (max 60 chars)
        Max 5 messages.
        """
        # Sort by weight descending
        sorted_msgs = sorted(messages, key=lambda x: x.get('weight', 0), reverse=True)
        top_msgs = sorted_msgs[:5]
        
        compressed = []
        for msg in top_msgs:
            source = msg.get('source', 'Unknown')
            weight = msg.get('weight', 'Low')
            content = msg.get('content', '')[:60] # Truncate to 60 chars
            compressed.append(f"[{source}|{weight}] {content}")
            
        return "\n".join(compressed)
        
    async def _log_usage(self, model: str, usage: dict):
        """Log token usage to DB for cost monitoring (Strategy 6 & Budget)"""
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
        
        # Approximate Bedrock costs per 1k tokens (Haiki/Sonnet vary, using Sonnet avg for now)
        cost_in = (input_tokens / 1000) * 0.003
        cost_out = (output_tokens / 1000) * 0.015
        total_cost = cost_in + cost_out
        
        logger.info(f"LLM Call: {model} | In: {input_tokens} Out: {output_tokens} | Cost: ${total_cost:.4f}")
        
        # Here we would insert into the llm_usage table
        # We need the DB connection passed in or managed globally. 
        # Skipping actual DB insert here to keep the file standalone for now.
