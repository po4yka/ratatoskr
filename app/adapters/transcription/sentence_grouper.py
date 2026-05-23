"""Group sherpa-onnx tokens into sentences with original-time timestamps.

Two tokenization conventions exist in the ecosystem and the grouper handles
both:

    * ``bpe`` -- SentencePiece BPE with the U+2581 word-start marker. Used by
      the Kroko streaming Zipformer (English) and the Apache-licensed
      vosk-sourced Zipformer-RU. ``join_tokens`` translates the marker back
      into a leading space.
    * ``char`` -- character-level tokens (typically Cyrillic for Russian
      models). Used by GigaAM-v3 e2e_rnnt. Tokens are concatenated verbatim
      and the recognizer is responsible for emitting its own spaces and
      punctuation.

Both modes split sentences on ``.!?`` boundaries against the same buffered
tokens + timestamps stream, so the orchestrator does not care which mode is
in use.
"""

from __future__ import annotations

from typing import Literal

from .types import Sentence

TokensMode = Literal["bpe", "char"]

_WORD_START_MARKER = "▁"  # the leading bullet sherpa-onnx uses for word starts
_SENTENCE_ENDS = (".", "!", "?")


def join_tokens(tokens: list[str], *, tokens_mode: TokensMode = "bpe") -> str:
    """Reconstruct text from a sequence of recognizer tokens.

    BPE mode honours the U+2581 word-start marker. Char mode joins tokens
    verbatim -- whitespace and punctuation come from the recognizer itself.
    """
    if tokens_mode == "char":
        return "".join(tokens).strip()

    out: list[str] = []
    for tok in tokens:
        if tok.startswith(_WORD_START_MARKER):
            out.append(" " + tok[1:])
        else:
            out.append(tok)
    return "".join(out).strip()


def group_into_sentences(
    tokens: list[str],
    timestamps: list[float],
    speed: float,
    *,
    tokens_mode: TokensMode = "bpe",
) -> tuple[Sentence, ...]:
    """Group tokens into sentences ending in .!? tagged with original-time start seconds.

    ``speed`` scales internal token timestamps back to original-audio time so
    the output remains correct even when ASR was run on a sped-up signal.
    """
    sentences: list[Sentence] = []
    buf_tokens: list[str] = []
    buf_start: float | None = None

    for tok, ts in zip(tokens, timestamps, strict=False):
        if not buf_tokens:
            buf_start = ts
        buf_tokens.append(tok)
        if tok and tok[-1] in _SENTENCE_ENDS:
            text = join_tokens(buf_tokens, tokens_mode=tokens_mode)
            if text:
                sentences.append(Sentence(start_sec=(buf_start or 0.0) * speed, text=text))
            buf_tokens = []
            buf_start = None

    if buf_tokens:
        text = join_tokens(buf_tokens, tokens_mode=tokens_mode)
        if text:
            sentences.append(Sentence(start_sec=(buf_start or 0.0) * speed, text=text))
    return tuple(sentences)


def format_mmss(seconds: float) -> str:
    """Format ``seconds`` as [MM:SS]-style zero-padded text."""
    sec = max(0.0, float(seconds))
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"
