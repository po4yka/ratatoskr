"""Auto-download and on-disk resolution for ASR + diarization models.

Differs from yapsnap's resolver in two ways:

    * paths come from ``TranscriptionConfig`` rather than XDG / per-OS cache dirs
    * a per-language bundle registry picks the right HF repo and normalizes
      upstream file names (GigaAM ships ``gigaam_v3_e2e_rnnt_encoder.onnx``
      etc.; we rename to plain ``encoder.onnx`` on disk so the recognizer
      loader stays language-agnostic).
"""

from __future__ import annotations

import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-language ASR bundle registry
# ---------------------------------------------------------------------------

_HF_BASE = "https://huggingface.co"
_USER_AGENT = "ratatoskr-transcription/0.1 (+https://github.com/)"
_HTTP_CHUNK = 1 << 16


@dataclass(frozen=True, slots=True)
class _AsrBundle:
    """One language preset.

    ``files`` is a tuple of ``(remote_name, local_name)`` pairs so we can
    rename on the fly. Every bundle MUST produce ``encoder.onnx``,
    ``decoder.onnx``, ``joiner.onnx``, and ``tokens.txt`` on disk -- that's
    the layout the recognizer loader expects.
    """

    hf_repo: str
    files: tuple[tuple[str, str], ...]
    license_note: str


_ASR_BUNDLES: dict[str, _AsrBundle] = {
    "en": _AsrBundle(
        hf_repo="csukuangfj/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06",
        files=(
            ("encoder.onnx", "encoder.onnx"),
            ("decoder.onnx", "decoder.onnx"),
            ("joiner.onnx", "joiner.onnx"),
            ("tokens.txt", "tokens.txt"),
        ),
        license_note="Apache-2.0 (Kroko English streaming Zipformer)",
    ),
    "ru": _AsrBundle(
        hf_repo="Smirnov75/GigaAM-v3-sherpa-onnx",
        files=(
            ("gigaam_v3_e2e_rnnt_encoder.onnx", "encoder.onnx"),
            ("gigaam_v3_e2e_rnnt_decoder.onnx", "decoder.onnx"),
            ("gigaam_v3_e2e_rnnt_joint.onnx", "joiner.onnx"),
            ("gigaam_v3_e2e_rnnt_tokens.txt", "tokens.txt"),
        ),
        license_note="MIT (GigaAM-v3 e2e RNN-T, ai-sage/GigaAM-v3)",
    ),
}


# ---------------------------------------------------------------------------
# Diarization models
# ---------------------------------------------------------------------------

_SEG_RELEASE_BASE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models"
)
# Upstream release name is intentionally misspelled "recongition"; do not "fix".
_EMB_RELEASE_BASE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
)

# segmentation key -> (archive filename, extracted dir name, license note)
SEGMENTATION_MODELS: dict[str, tuple[str, str, str]] = {
    "pyannote": (
        "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
        "sherpa-onnx-pyannote-segmentation-3-0",
        "CC-BY-4.0 (attribution)",
    ),
    "reverb": (
        "sherpa-onnx-reverb-diarization-v1.tar.bz2",
        "sherpa-onnx-reverb-diarization-v1",
        "NON-COMMERCIAL -- see Rev.ai model card before any commercial use",
    ),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ModelDownloadError(RuntimeError):
    """Raised when an ASR or diarization model file fails to download."""


class ModelDirectoryError(RuntimeError):
    """Raised when a configured model directory is missing required files."""


class UnknownDiarizationModelError(ValueError):
    """Raised when ``segmentation_key`` is not a known diarization model."""


class UnknownLanguageError(ValueError):
    """Raised when ``language`` has no registered ASR bundle."""


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def _download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` atomically with a sanity check on size.

    Writes to ``<dest>.part`` then renames so a partial download cannot
    masquerade as a valid file. Refuses ONNX payloads smaller than 1 KiB --
    they are almost certainly HTTP error pages, not real models.
    """
    logger.info("transcription_model_download_start", extra={"url": url, "dest": str(dest)})
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        msg = f"refusing non-HTTPS model download URL: {url}"
        raise ModelDownloadError(msg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = urllib.request.urlopen(req)  # nosec B310
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code} {exc.reason} for {url}"
        raise ModelDownloadError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"network error for {url}: {exc.reason}"
        raise ModelDownloadError(msg) from exc

    bytes_read = 0
    with resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(_HTTP_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            bytes_read += len(chunk)

    size = tmp.stat().st_size
    if dest.suffix == ".onnx" and size < 1024:
        tmp.unlink(missing_ok=True)
        msg = f"downloaded {dest.name} is only {size} bytes -- likely an error page, not a model"
        raise ModelDownloadError(msg)
    tmp.replace(dest)
    logger.info(
        "transcription_model_download_complete",
        extra={"dest": str(dest), "bytes": bytes_read},
    )


def _extract_tar_bz2(archive: Path, dest_dir: Path) -> None:
    """Extract ``archive`` into ``dest_dir.parent`` with path-traversal protection."""
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tf:
        base = dest_dir.parent.resolve()
        for member in tf.getmembers():
            target = (base / member.name).resolve()
            if not str(target).startswith(str(base)):
                msg = f"unsafe path in archive: {member.name}"
                raise ModelDownloadError(msg)
        tf.extractall(dest_dir.parent, filter="data")


# ---------------------------------------------------------------------------
# ASR model resolution
# ---------------------------------------------------------------------------


def find_model_file(model_dir: Path, base: str) -> Path:
    """Return ``<model_dir>/<base>.int8.onnx`` if present, else ``<base>.onnx``.

    Preferring INT8 mirrors yapsnap's behaviour for custom model directories
    that ship both variants.
    """
    for name in (f"{base}.int8.onnx", f"{base}.onnx"):
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    msg = f"No {base}(.int8).onnx found in {model_dir}"
    raise ModelDirectoryError(msg)


def ensure_asr_model(model_path: Path, language: str = "en") -> Path:
    """Ensure the ASR model for ``language`` exists under ``model_path``.

    If ``model_path`` already contains ``tokens.txt`` we treat it as a
    user-supplied model directory and skip the download entirely. Otherwise
    the per-language bundle is downloaded and files are renamed to the
    canonical ``encoder/decoder/joiner/tokens.txt`` layout.
    """
    if language not in _ASR_BUNDLES:
        msg = f"unknown TRANSCRIPTION_LANGUAGE {language!r}; known: {sorted(_ASR_BUNDLES)}"
        raise UnknownLanguageError(msg)
    bundle = _ASR_BUNDLES[language]

    model_path.mkdir(parents=True, exist_ok=True)
    if (model_path / "tokens.txt").is_file():
        return model_path

    missing = [
        (remote, local) for (remote, local) in bundle.files if not (model_path / local).is_file()
    ]
    if not missing:
        return model_path

    logger.info(
        "transcription_asr_model_bootstrap",
        extra={
            "language": language,
            "repo": bundle.hf_repo,
            "license": bundle.license_note,
            "model_path": str(model_path),
            "missing": [local for (_remote, local) in missing],
        },
    )
    for remote_name, local_name in missing:
        url = f"{_HF_BASE}/{bundle.hf_repo}/resolve/main/{remote_name}"
        try:
            _download(url, model_path / local_name)
        except ModelDownloadError:
            raise
        except Exception as exc:  # narrow to ModelDownloadError for callers
            msg = f"failed to download {remote_name} from {url}: {exc}"
            raise ModelDownloadError(msg) from exc
    return model_path


# ---------------------------------------------------------------------------
# Diarization model resolution
# ---------------------------------------------------------------------------


def ensure_diarization_models(
    *,
    segmentation_key: str,
    embedding_model_filename: str,
    cache_dir: Path,
) -> tuple[Path, Path]:
    """Ensure segmentation + embedding ONNX models exist; return their paths."""
    if segmentation_key not in SEGMENTATION_MODELS:
        msg = (
            f"unknown diarization segmentation model {segmentation_key!r}; "
            f"choose from {sorted(SEGMENTATION_MODELS)}"
        )
        raise UnknownDiarizationModelError(msg)

    archive_name, extract_dir_name, license_note = SEGMENTATION_MODELS[segmentation_key]
    cache_dir.mkdir(parents=True, exist_ok=True)

    seg_dir = cache_dir / extract_dir_name
    seg_onnx = seg_dir / "model.onnx"
    if not seg_onnx.is_file():
        if segmentation_key == "reverb":
            logger.warning(
                "transcription_diarization_license_notice",
                extra={
                    "model": segmentation_key,
                    "license": license_note,
                    "message": (
                        "reverb-diarization-v1 is NON-COMMERCIAL licensed; "
                        "verify the Rev.ai model card permits your use case."
                    ),
                },
            )
        archive_path = cache_dir / archive_name
        if not archive_path.is_file():
            _download(f"{_SEG_RELEASE_BASE}/{archive_name}", archive_path)
        logger.info("transcription_diarization_extract", extra={"archive": str(archive_path)})
        _extract_tar_bz2(archive_path, seg_dir)
        archive_path.unlink(missing_ok=True)
        if not seg_onnx.is_file():
            msg = f"after extracting {archive_name}, {seg_onnx} is missing"
            raise ModelDownloadError(msg)

    emb_onnx = cache_dir / embedding_model_filename
    if not emb_onnx.is_file():
        _download(f"{_EMB_RELEASE_BASE}/{embedding_model_filename}", emb_onnx)

    return seg_onnx, emb_onnx
