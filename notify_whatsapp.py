import os
import requests
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

def send_whatsapp_update(message: str):
    try:
        # OpenClaw automatisiertes Messaging
        # Sende Nachricht über das interne Gateway
        # Wir nutzen die systemweite Session-Kommunikation
        import subprocess
        subprocess.run(["openclaw", "message", "discord", "WhatsApp", message], check=False)
        logger.info(f"WhatsApp-Update gesendet: {message}")
    except Exception as e:
        logger.error(f"Fehler beim Senden der WhatsApp: {e}")
