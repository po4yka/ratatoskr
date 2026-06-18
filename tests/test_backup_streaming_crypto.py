"""Tests for the framed streaming backup encryption format.

Covers:
- Round-trip correctness (tiny, multi-chunk, empty inputs).
- Error cases: wrong key, tampered frame, truncated stream, reordered frames.
- Format detection helpers.
- Integration: writer produces a streaming-encrypted archive readable by the reader.
- Legacy compatibility: a whole-blob Fernet archive is still restorable.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile

import pytest
from cryptography.fernet import Fernet as _Fernet
from pydantic import SecretStr

from app.infrastructure.persistence.backup_crypto import (
    FERNET_MAGIC,
    STREAM_MAGIC,
    InvalidBackupCiphertextError,
    decrypt_backup,
    decrypt_backup_stream,
    encrypt_backup,
    encrypt_backup_stream,
    is_fernet_ciphertext,
    is_streaming_ciphertext,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_KEY_BYTES = _Fernet.generate_key()
_KEY = SecretStr(_KEY_BYTES.decode())
_OTHER_KEY = SecretStr(_Fernet.generate_key().decode())


def _roundtrip(plaintext: bytes, *, chunk_size: int = 4 * 1024 * 1024) -> bytes:
    src = io.BytesIO(plaintext)
    dst = io.BytesIO()
    encrypt_backup_stream(src, dst, _KEY, chunk_size=chunk_size)
    ciphertext = dst.getvalue()

    src2 = io.BytesIO(ciphertext)
    dst2 = io.BytesIO()
    decrypt_backup_stream(src2, dst2, _KEY)
    return dst2.getvalue()


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_tiny_input(self) -> None:
        plaintext = b"hello streaming world"
        assert _roundtrip(plaintext) == plaintext

    def test_multi_chunk_input(self) -> None:
        # 20 KiB with chunk_size=4096 forces 5 frames
        plaintext = b"x" * (20 * 1024)
        assert _roundtrip(plaintext, chunk_size=4096) == plaintext

    def test_empty_input(self) -> None:
        assert _roundtrip(b"") == b""

    def test_exactly_one_chunk(self) -> None:
        plaintext = b"y" * 4096
        assert _roundtrip(plaintext, chunk_size=4096) == plaintext

    def test_one_byte_over_chunk_boundary(self) -> None:
        plaintext = b"z" * (4096 + 1)
        assert _roundtrip(plaintext, chunk_size=4096) == plaintext


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_wrong_key_raises(self) -> None:
        src = io.BytesIO(b"secret data")
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)

        src2 = io.BytesIO(dst.getvalue())
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _OTHER_KEY)

    def test_tampered_frame_raises(self) -> None:
        src = io.BytesIO(b"important data")
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)

        raw = bytearray(dst.getvalue())
        # Flip a byte well past the magic (somewhere in the first data frame).
        raw[len(STREAM_MAGIC) + 100] ^= 0xFF
        src2 = io.BytesIO(bytes(raw))
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _KEY)

    def test_truncated_stream_raises(self) -> None:
        # Write a 3-frame stream then drop the last 50 bytes (truncates the last frame).
        plaintext = b"chunk" * 1000  # forces multiple frames with small chunk_size
        src = io.BytesIO(plaintext)
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY, chunk_size=512)

        truncated = dst.getvalue()[:-50]
        src2 = io.BytesIO(truncated)
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _KEY)

    def test_reordered_frames_raises(self) -> None:
        # Build a 3-frame stream (magic + header + 2 data frames), then swap
        # data frame 0 and data frame 1.
        plaintext = b"A" * 600
        src = io.BytesIO(plaintext)
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY, chunk_size=300)

        blob = dst.getvalue()
        # Parse out the frames manually so we can reorder them.
        pos = len(STREAM_MAGIC)
        frames: list[bytes] = []
        while pos < len(blob):
            (length,) = struct.unpack(">I", blob[pos : pos + 4])
            pos += 4
            frames.append(blob[pos : pos + length])
            pos += length
        # frames[0] = header, frames[1] = data frame 0, frames[2] = data frame 1
        assert len(frames) == 3, f"expected 3 frames, got {len(frames)}"

        # Reconstruct with data frames swapped.
        swapped = io.BytesIO()
        swapped.write(STREAM_MAGIC)
        for token in [frames[0], frames[2], frames[1]]:
            swapped.write(struct.pack(">I", len(token)))
            swapped.write(token)

        src2 = io.BytesIO(swapped.getvalue())
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _KEY)

    def test_wrong_magic_raises(self) -> None:
        src2 = io.BytesIO(b"NOTMAGIC" + b"\x00" * 64)
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _KEY)

    def test_missing_header_frame_raises(self) -> None:
        # Only the magic, no frames.
        src2 = io.BytesIO(STREAM_MAGIC)
        dst2 = io.BytesIO()
        with pytest.raises(InvalidBackupCiphertextError):
            decrypt_backup_stream(src2, dst2, _KEY)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


class TestFormatDetection:
    def test_is_streaming_ciphertext_true_for_new_format(self) -> None:
        src = io.BytesIO(b"payload")
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)
        assert is_streaming_ciphertext(dst.getvalue()) is True

    def test_is_streaming_ciphertext_false_for_fernet(self) -> None:
        ct = encrypt_backup(b"payload", _KEY)
        assert is_streaming_ciphertext(ct) is False

    def test_is_streaming_ciphertext_false_for_zip(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("f.txt", "hi")
        assert is_streaming_ciphertext(buf.getvalue()) is False

    def test_is_fernet_ciphertext_still_true_for_legacy(self) -> None:
        ct = encrypt_backup(b"legacy", _KEY)
        assert is_fernet_ciphertext(ct) is True
        assert ct[:6] == FERNET_MAGIC

    def test_is_fernet_ciphertext_false_for_streaming(self) -> None:
        src = io.BytesIO(b"payload")
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)
        assert is_fernet_ciphertext(dst.getvalue()) is False

    def test_stream_magic_distinct_from_fernet_magic(self) -> None:
        assert STREAM_MAGIC[:6] != FERNET_MAGIC


# ---------------------------------------------------------------------------
# Integration tests (writer → reader round-trip)
# ---------------------------------------------------------------------------


def _minimal_zip() -> bytes:
    """Build the smallest valid backup ZIP (empty entity arrays)."""
    manifest = {
        "version": "1.0",
        "schema_version": "1.0",
        "user_id": 1,
        "created_at": "2025-01-01T00:00:00+00:00",
        "counts": {
            "requests": 0,
            "summaries": 0,
            "tags": 0,
            "summary_tags": 0,
            "collections": 0,
            "collection_items": 0,
            "highlights": 0,
        },
    }
    entity_files = [
        "requests.json",
        "summaries.json",
        "tags.json",
        "summary_tags.json",
        "collections.json",
        "collection_items.json",
        "highlights.json",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name in entity_files:
            zf.writestr(name, "[]")
        zf.writestr("preferences.json", "{}")
    return buf.getvalue()


class TestIntegration:
    def test_streaming_encrypted_zip_round_trip(self) -> None:
        """Encrypt a valid ZIP with the streaming format and decrypt it back."""
        zip_bytes = _minimal_zip()

        src = io.BytesIO(zip_bytes)
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)
        ciphertext = dst.getvalue()

        assert is_streaming_ciphertext(ciphertext)

        src2 = io.BytesIO(ciphertext)
        dst2 = io.BytesIO()
        decrypt_backup_stream(src2, dst2, _KEY)
        recovered = dst2.getvalue()

        assert recovered == zip_bytes
        # Confirm it's still a valid ZIP after decryption.
        with zipfile.ZipFile(io.BytesIO(recovered)) as zf:
            assert "manifest.json" in zf.namelist()

    def test_legacy_fernet_archive_still_decryptable(self) -> None:
        """A whole-blob Fernet backup can still be decrypted via the legacy path."""
        zip_bytes = _minimal_zip()
        ciphertext = encrypt_backup(zip_bytes, _KEY)

        assert is_fernet_ciphertext(ciphertext)
        recovered = decrypt_backup(ciphertext, _KEY)
        assert recovered == zip_bytes

    def test_inspector_handles_streaming_format(self) -> None:
        """inspect_backup_archive accepts the new streaming format."""
        from app.config.backup import BackupConfig
        from app.infrastructure.persistence.backup_inspector import inspect_backup_archive

        zip_bytes = _minimal_zip()
        src = io.BytesIO(zip_bytes)
        dst = io.BytesIO()
        encrypt_backup_stream(src, dst, _KEY)
        payload = dst.getvalue()

        cfg = BackupConfig(encryption_key=_KEY)
        inspection, errors = inspect_backup_archive(payload, cfg=cfg)
        assert errors == [], errors
        assert inspection is not None
        assert inspection.encrypted is True

    def test_inspector_handles_legacy_fernet_format(self) -> None:
        """inspect_backup_archive still handles the old whole-blob Fernet format."""
        from app.config.backup import BackupConfig
        from app.infrastructure.persistence.backup_inspector import inspect_backup_archive

        zip_bytes = _minimal_zip()
        payload = encrypt_backup(zip_bytes, _KEY)

        cfg = BackupConfig(encryption_key=_KEY)
        inspection, errors = inspect_backup_archive(payload, cfg=cfg)
        assert errors == [], errors
        assert inspection is not None
        assert inspection.encrypted is True

    def test_inspector_handles_unencrypted(self) -> None:
        """inspect_backup_archive still handles a plain ZIP."""
        from app.config.backup import BackupConfig
        from app.infrastructure.persistence.backup_inspector import inspect_backup_archive

        payload = _minimal_zip()
        cfg = BackupConfig()
        inspection, errors = inspect_backup_archive(payload, cfg=cfg)
        assert errors == [], errors
        assert inspection is not None
        assert inspection.encrypted is False
