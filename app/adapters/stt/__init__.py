"""Speech-to-text adapter surface."""

from __future__ import annotations

from .openai import OpenAIWhisperSTTClient, OpenAIWhisperSTTError
from .protocols import STTClientProtocol

__all__ = [
    "OpenAIWhisperSTTClient",
    "OpenAIWhisperSTTError",
    "STTClientProtocol",
]
