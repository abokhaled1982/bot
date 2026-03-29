import os
import subprocess
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

def send_whatsapp_update(message: str):
    try:
        # Korrigierter Aufruf: 'openclaw message send' mit --channel und --target
        # Wir senden an die im System hinterlegte Standard-Nummer oder nutzen das Gateway direkt
        subprocess.run(["openclaw", "message", "send", "--channel", "whatsapp", "--target", "+4917676550606", "--message", message], check=True)
        logger.info(f"WhatsApp-Update erfolgreich gesendet.")
    except Exception as e:
        logger.error(f"Fehler beim Senden der WhatsApp: {e}")
