"""
src/notifications/__init__.py — Notifier-Fassade.

Aktuell wird nur die WhatsApp-Bridge unterstuetzt. Erzeuge einen Notifier
ueber `from src.notifications import get_notifier`. Faellt die Bridge aus,
wird das Trading davon NICHT beeintraechtigt — Notifier-Fehler sind
protokolliert, aber nicht toedlich.
"""
from __future__ import annotations

from .whatsapp import WhatsAppNotifier, NullNotifier, get_notifier

__all__ = ["WhatsAppNotifier", "NullNotifier", "get_notifier"]
