"""Auto-download and on-disk resolution for ASR + diarization models.

Differs from yapsnap's resolver: paths come from ``TranscriptionConfig`` rather
than XDG / per-OS cache dirs, and the downloader uses ratatoskr's structured
logger instead of writing progress to stderr.
"""

from __future__ import annotations

import tarfile
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Kroko English streaming ASR (default)
# ---------------------------------------------------------------------------

_HF_BASE = "https://huggingface.co"
_DEFAULT_ASR_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06"
_DEFAULT_ASR_FILES = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")

_USER_AGENT = "ratatoskr-transcription/0.1 (+https://github.com/)"
_HTTP_CHUNK = 1 << 16


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
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        resp = urllib.request.urlopen(req)
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
        tf.extractall(dest_dir.parent)


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


def ensure_asr_model(model_path: Path) -> Path:
    """Ensure the default Kroko English model exists under ``model_path``.

    If ``model_path`` already contains ``tokens.txt`` we treat it as a
    user-supplied model directory and skip the download entirely (the same
    behaviour as yapsnap's ``KROKO_MODEL`` override).
    """
    model_path.mkdir(parents=True, exist_ok=True)
    if (model_path / "tokens.txt").is_file():
        return model_path

    missing = [name for name in _DEFAULT_ASR_FILES if not (model_path / name).is_file()]
    if not missing:
        return model_path

    logger.info(
        "transcription_asr_model_bootstrap",
        extra={"model_path": str(model_path), "missing": missing},
    )
    for fname in missing:
        url = f"{_HF_BASE}/{_DEFAULT_ASR_REPO}/resolve/main/{fname}"
        try:
            _download(url, model_path / fname)
        except ModelDownloadError:
            raise
        except Exception as exc:  # narrow to ModelDownloadError for callers
            msg = f"failed to download {fname} from {url}: {exc}"
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
