"""Russian-language path: GigaAM-v3 offline backend + char tokens grouping.

Engine and downloads are mocked at the seam; these tests run without
sherpa-onnx and without network access.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.adapters.transcription.asr_engine import OfflineAsrEngine
from app.adapters.transcription.model_resolver import (
    _ASR_BUNDLES,
    UnknownLanguageError,
    ensure_asr_model,
)
from app.adapters.transcription.sentence_grouper import (
    group_into_sentences,
    join_tokens,
)
from app.config.transcription import TranscriptionConfig


# ---------------------------------------------------------------------------
# Language preset + override behaviour on TranscriptionConfig
# ---------------------------------------------------------------------------


def test_english_default_preset() -> None:
    cfg = TranscriptionConfig()
    assert cfg.language == "en"
    assert cfg.backend == "streaming"
    assert cfg.tokens_mode == "bpe"


def test_russian_preset_picks_offline_char() -> None:
    cfg = TranscriptionConfig(language="ru")
    assert cfg.language == "ru"
    assert cfg.backend == "offline_transducer"
    assert cfg.tokens_mode == "char"


def test_explicit_overrides_beat_language_preset() -> None:
    cfg = TranscriptionConfig(
        language="en",
        backend_override="offline_transducer",
        tokens_mode_override="char",
    )
    assert cfg.backend == "offline_transducer"
    assert cfg.tokens_mode == "char"


def test_unknown_language_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="TRANSCRIPTION_LANGUAGE"):
        TranscriptionConfig(language="xx")


# ---------------------------------------------------------------------------
# Char-mode sentence grouper
# ---------------------------------------------------------------------------


def test_char_mode_join_concatenates_verbatim() -> None:
    tokens = ["п", "р", "и", "в", "е", "т", ".", " ", "к", "а", "к"]
    assert join_tokens(tokens, tokens_mode="char") == "привет. как"


def test_bpe_mode_unchanged() -> None:
    # ▁ marks word start -> gets a leading space; non-▁ tokens are joined verbatim.
    tokens = ["▁hello", ",", "▁world", "."]
    assert join_tokens(tokens) == "hello, world."


def test_char_grouping_splits_on_punctuation() -> None:
    tokens = ["п", "р", "и", "в", "е", "т", ".", " ", "д", "а", "."]
    times = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.8, 0.9]
    sentences = group_into_sentences(tokens, times, speed=1.0, tokens_mode="char")
    assert len(sentences) == 2
    assert sentences[0].text == "привет."
    assert sentences[1].text == "да."
    assert sentences[0].start_sec == 0.0
    # speed=1.0; second sentence starts at the timestamp of its first token
    assert sentences[1].start_sec >= 0.6


def test_char_grouping_respects_speed_scaling() -> None:
    tokens = ["а", "."]
    times = [1.0, 2.0]
    sentences = group_into_sentences(tokens, times, speed=2.0, tokens_mode="char")
    assert len(sentences) == 1
    assert sentences[0].start_sec == 2.0  # 1.0 token-time * 2.0 speed


# ---------------------------------------------------------------------------
# GigaAM bundle registry + file renaming
# ---------------------------------------------------------------------------


def test_russian_bundle_normalizes_gigaam_filenames() -> None:
    """GigaAM ships gigaam_v3_e2e_rnnt_*; resolver must produce plain names."""
    bundle = _ASR_BUNDLES["ru"]
    locals_ = {local for (_remote, local) in bundle.files}
    assert locals_ == {"encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"}
    # Remote names should all carry the upstream prefix
    remotes = [remote for (remote, _local) in bundle.files]
    assert all(name.startswith("gigaam_v3_e2e_rnnt_") for name in remotes)


def test_russian_bundle_repo_and_license() -> None:
    bundle = _ASR_BUNDLES["ru"]
    assert bundle.hf_repo == "Smirnov75/GigaAM-v3-sherpa-onnx"
    assert "MIT" in bundle.license_note


def test_english_bundle_layout_unchanged() -> None:
    bundle = _ASR_BUNDLES["en"]
    # English uses identity rename (remote == local)
    for remote, local in bundle.files:
        assert remote == local


def test_ensure_asr_model_skips_when_tokens_present(tmp_path: Path) -> None:
    """If tokens.txt already exists, treat as custom model and skip download."""
    (tmp_path / "tokens.txt").write_text("dummy")
    with patch(
        "app.adapters.transcription.model_resolver._download"
    ) as download_mock:
        ensure_asr_model(tmp_path, "ru")
    download_mock.assert_not_called()


def test_ensure_asr_model_downloads_renamed_ru_files(tmp_path: Path) -> None:
    """RU path should fetch from Smirnov75 with upstream names + write canonical names."""
    download_calls: list[tuple[str, str]] = []

    def fake_download(url: str, dest: Path) -> None:
        download_calls.append((url, dest.name))
        dest.write_text("ok")

    with patch(
        "app.adapters.transcription.model_resolver._download",
        side_effect=fake_download,
    ):
        ensure_asr_model(tmp_path, "ru")

    assert len(download_calls) == 4
    remote_names = [url.rsplit("/", 1)[-1] for (url, _local) in download_calls]
    local_names = [local for (_url, local) in download_calls]
    assert all(name.startswith("gigaam_v3_e2e_rnnt_") for name in remote_names)
    assert set(local_names) == {"encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"}
    assert all("Smirnov75/GigaAM-v3-sherpa-onnx" in url for (url, _local) in download_calls)


def test_ensure_asr_model_rejects_unknown_language(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(UnknownLanguageError, match="unknown TRANSCRIPTION_LANGUAGE"):
        ensure_asr_model(tmp_path, "xx")


# ---------------------------------------------------------------------------
# OfflineAsrEngine
# ---------------------------------------------------------------------------


def _patch_sherpa_onnx(monkeypatch: object) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Install a fake sherpa_onnx module that records construction args."""
    import sys

    fake = MagicMock()
    recognizer = MagicMock()
    stream = MagicMock()
    stream.result = MagicMock(text="распознанный текст.", tokens=[], timestamps=[])

    recognizer.create_stream = MagicMock(return_value=stream)
    recognizer.decode_streams = MagicMock()
    fake.OfflineRecognizer.from_transducer = MagicMock(return_value=recognizer)

    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)  # type: ignore[attr-defined]
    return fake, recognizer, stream


def test_offline_engine_calls_offline_recognizer_loader(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    (tmp_path / "encoder.onnx").write_bytes(b"x" * 2048)
    (tmp_path / "decoder.onnx").write_bytes(b"x" * 2048)
    (tmp_path / "joiner.onnx").write_bytes(b"x" * 2048)
    (tmp_path / "tokens.txt").write_text("dummy")

    fake_sherpa, _recognizer, _stream = _patch_sherpa_onnx(monkeypatch)

    engine = OfflineAsrEngine(model_dir=tmp_path, num_threads=2, tokens_mode="char")
    import numpy as np

    text, sentences = engine.transcribe_sync(
        np.ones(16000, dtype=np.float32), speed=1.0
    )

    fake_sherpa.OfflineRecognizer.from_transducer.assert_called_once()
    kwargs = fake_sherpa.OfflineRecognizer.from_transducer.call_args.kwargs
    assert kwargs["encoder"].endswith("encoder.onnx")
    assert kwargs["joiner"].endswith("joiner.onnx")
    assert kwargs["tokens"].endswith("tokens.txt")
    assert kwargs["sample_rate"] == 16000
    assert kwargs["feature_dim"] == 80
    assert text == "распознанный текст."
    # No timestamps were returned -> sentences is None
    assert sentences is None


def test_offline_engine_returns_empty_for_zero_samples(tmp_path: Path) -> None:
    import numpy as np

    engine = OfflineAsrEngine(model_dir=tmp_path, num_threads=1, tokens_mode="char")
    text, sentences = engine.transcribe_sync(np.array([], dtype=np.float32), speed=1.0)
    assert text == ""
    assert sentences == ()
