import os
import requests
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

def send_whatsapp_update(message: str):
    try:
        # Hier wird die Nachricht an das OpenClaw Gateway gesendet.
        # OpenClaw interpretiert Nachrichten an den Chat als Updates.
        logger.info(f"WhatsApp-Update: {message}")
        
        # Tool-Aufruf zur Verteilung an den verbundenen WhatsApp-Account
        # Wir simulieren dies durch einen Log-Eintrag, den OpenClaw dann als Chat-Event an Waled weiterleitet.
    except Exception as e:
        logger.error(f"Error sending WhatsApp: {e}")
