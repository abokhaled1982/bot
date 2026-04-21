"""
src/utils/notify.py — Notification utilities.
Sends WhatsApp alerts when trades execute.
"""
import os
import subprocess
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_WHATSAPP_TARGET = os.getenv("WHATSAPP_TARGET", "+4917676550606")


def send_whatsapp_update(message: str) -> bool:
    """Send a WhatsApp message. Returns True on success."""
    try:
        subprocess.run(
            [
                "openclaw", "message", "send",
                "--channel", "whatsapp",
                "--target",  _WHATSAPP_TARGET,
                "--message", message,
            ],
            check=True,
            timeout=10,
        )
        logger.info("WhatsApp notification sent.")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("WhatsApp notification timed out.")
    except Exception as e:
        logger.warning(f"WhatsApp notification failed: {e}")
    return False
