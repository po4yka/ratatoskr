"""Fernet encryption/decryption for backup archives at rest.

Two on-disk formats are supported:

1. Legacy whole-blob Fernet (FERNET_MAGIC prefix).
   Written by encrypt_backup / read by decrypt_backup.

2. Framed streaming format (STREAM_MAGIC prefix).
   Written by encrypt_backup_stream / read by decrypt_backup_stream.
   Layout:
     STREAM_MAGIC (8 bytes)
     || header_frame
     || data_frame_0
     || data_frame_1
     ...
   Each frame on disk: struct.pack(">I", len(token)) || token
   header_frame token  = Fernet.encrypt(b"RZHDR1" || struct.pack(">Q", total_plaintext_len))
   data_frame_i token  = Fernet.encrypt(struct.pack(">I", i) || chunk_bytes)
   Default chunk size: 4 MiB.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

__all__ = [
    "FERNET_MAGIC",
    "STREAM_MAGIC",
    "InvalidBackupCiphertextError",
    "MissingBackupEncryptionKeyError",
    "decrypt_backup",
    "decrypt_backup_stream",
    "encrypt_backup",
    "encrypt_backup_stream",
    "is_fernet_ciphertext",
    "is_streaming_ciphertext",
]

FERNET_MAGIC = b"gAAAAA"
STREAM_MAGIC = b"RZSTRM01"

_HEADER_PREFIX = b"RZHDR1"
_DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB


class InvalidBackupCiphertextError(ValueError):
    """Raised when a backup ciphertext cannot be decrypted (wrong key or corruption)."""


class MissingBackupEncryptionKeyError(RuntimeError):
    """Raised by callers when BACKUP_ENCRYPTION_KEY is not configured and encryption is needed."""


def _fernet(key: SecretStr) -> Fernet:
    from cryptography.fernet import Fernet

    return Fernet(key.get_secret_value().encode())


def is_fernet_ciphertext(data: bytes) -> bool:
    """Return True if *data* starts with the Fernet token prefix."""
    return data[:6] == FERNET_MAGIC


def is_streaming_ciphertext(data: bytes) -> bool:
    """Return True if *data* starts with the streaming format magic bytes."""
    return data[:8] == STREAM_MAGIC


def encrypt_backup(zip_bytes: bytes, key: SecretStr) -> bytes:
    """Fernet-encrypt *zip_bytes* and return opaque ciphertext bytes."""
    return _fernet(key).encrypt(zip_bytes)


def decrypt_backup(data: bytes, key: SecretStr) -> bytes:
    """Decrypt Fernet *data* and return raw ZIP bytes.

    Raises InvalidBackupCiphertextError on wrong key or corrupted ciphertext.
    """
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(key).decrypt(data)
    except InvalidToken as exc:
        raise InvalidBackupCiphertextError(
            "Could not decrypt backup archive (wrong key or corrupted ciphertext)"
        ) from exc


def _write_frame(dst: BinaryIO, token: bytes) -> None:
    """Write a length-prefixed frame to *dst*."""
    dst.write(struct.pack(">I", len(token)))
    dst.write(token)


def _read_frame(src: BinaryIO) -> bytes | None:
    """Read a length-prefixed frame from *src*. Returns None at clean EOF."""
    length_bytes = src.read(4)
    if not length_bytes:
        return None
    if len(length_bytes) < 4:
        msg = "Truncated frame length prefix in streaming backup"
        raise InvalidBackupCiphertextError(msg)
    (length,) = struct.unpack(">I", length_bytes)
    token = src.read(length)
    if len(token) < length:
        msg = "Truncated frame data in streaming backup"
        raise InvalidBackupCiphertextError(msg)
    return token


def encrypt_backup_stream(
    src: BinaryIO,
    dst: BinaryIO,
    key: SecretStr,
    *,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> None:
    """Stream-encrypt *src* to *dst* using the framed streaming format.

    Reads *src* in *chunk_size* chunks, encrypts each independently with
    Fernet, and writes length-prefixed frames to *dst*.  Peak memory is
    bounded to one chunk plus its Fernet token.
    """
    f = _fernet(key)

    # Measure total plaintext length so the header can include it.
    start = src.tell()
    src.seek(0, 2)  # seek to end
    total_len = src.tell() - start
    src.seek(start)

    dst.write(STREAM_MAGIC)

    # Write header frame.
    header_plaintext = _HEADER_PREFIX + struct.pack(">Q", total_len)
    _write_frame(dst, f.encrypt(header_plaintext))

    # Write data frames.
    index = 0
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        frame_plaintext = struct.pack(">I", index) + chunk
        _write_frame(dst, f.encrypt(frame_plaintext))
        index += 1


def decrypt_backup_stream(src: BinaryIO, dst: BinaryIO, key: SecretStr) -> None:
    """Stream-decrypt a streaming backup from *src* to *dst*.

    Verifies:
    - Magic bytes match STREAM_MAGIC.
    - Header frame decrypts and contains the expected total plaintext length.
    - Each data frame decrypts successfully and carries the expected sequential index.
    - Total bytes written match the declared total_plaintext_len.

    Raises InvalidBackupCiphertextError on any verification failure.
    """
    from cryptography.fernet import InvalidToken

    f = _fernet(key)

    magic = src.read(8)
    if magic != STREAM_MAGIC:
        msg = "Not a streaming backup archive (wrong magic bytes)"
        raise InvalidBackupCiphertextError(msg)

    # Decrypt header frame.
    header_token = _read_frame(src)
    if header_token is None:
        msg = "Missing header frame in streaming backup"
        raise InvalidBackupCiphertextError(msg)
    try:
        header_plaintext = f.decrypt(header_token)
    except InvalidToken as exc:
        raise InvalidBackupCiphertextError(
            "Could not decrypt streaming backup header (wrong key or corrupted)"
        ) from exc
    if not header_plaintext.startswith(_HEADER_PREFIX):
        msg = "Streaming backup header has wrong prefix"
        raise InvalidBackupCiphertextError(msg)
    (total_len,) = struct.unpack(">Q", header_plaintext[len(_HEADER_PREFIX) :])

    # Decrypt data frames sequentially.
    expected_index = 0
    bytes_written = 0
    while True:
        token = _read_frame(src)
        if token is None:
            break
        try:
            frame_plaintext = f.decrypt(token)
        except InvalidToken as exc:
            raise InvalidBackupCiphertextError(
                f"Could not decrypt streaming backup data frame {expected_index} "
                "(wrong key or corrupted)"
            ) from exc
        if len(frame_plaintext) < 4:
            msg = f"Data frame {expected_index} too short to contain index prefix"
            raise InvalidBackupCiphertextError(msg)
        (embedded_index,) = struct.unpack(">I", frame_plaintext[:4])
        if embedded_index != expected_index:
            msg = (
                f"Streaming backup frame reordering detected: "
                f"expected index {expected_index}, got {embedded_index}"
            )
            raise InvalidBackupCiphertextError(msg)
        chunk = frame_plaintext[4:]
        dst.write(chunk)
        bytes_written += len(chunk)
        expected_index += 1

    if bytes_written != total_len:
        msg = (
            f"Streaming backup truncated or padded: expected {total_len} bytes, got {bytes_written}"
        )
        raise InvalidBackupCiphertextError(msg)
