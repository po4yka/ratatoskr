"""Group sherpa-onnx BPE tokens into sentences with original-time timestamps."""

from __future__ import annotations

from .types import Sentence

_WORD_START_MARKER = "▁"  # the leading bullet sherpa-onnx uses for word starts
_SENTENCE_ENDS = (".", "!", "?")


def join_tokens(tokens: list[str]) -> str:
    """Reconstruct text from BPE-style tokens that use the U+2581 word-start marker."""
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
) -> tuple[Sentence, ...]:
    """Group tokens into sentences ending in .!? tagged with original-time start seconds.

    `speed` scales internal token timestamps back to original-audio time so the
    output remains correct even when ASR was run on a sped-up signal.
    """
    sentences: list[Sentence] = []
    buf_tokens: list[str] = []
    buf_start: float | None = None

    for tok, ts in zip(tokens, timestamps, strict=False):
        if not buf_tokens:
            buf_start = ts
        buf_tokens.append(tok)
        if tok and tok[-1] in _SENTENCE_ENDS:
            text = join_tokens(buf_tokens)
            if text:
                sentences.append(Sentence(start_sec=(buf_start or 0.0) * speed, text=text))
            buf_tokens = []
            buf_start = None

    if buf_tokens:
        text = join_tokens(buf_tokens)
        if text:
            sentences.append(Sentence(start_sec=(buf_start or 0.0) * speed, text=text))
    return tuple(sentences)


def format_mmss(seconds: float) -> str:
    """Format `seconds` as [MM:SS]-style zero-padded text."""
    sec = max(0.0, float(seconds))
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"
