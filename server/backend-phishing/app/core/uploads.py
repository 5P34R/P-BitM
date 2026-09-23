from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from pathlib import PurePath

from fastapi import UploadFile


DEFAULT_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_DATA_URL_PREFIX = "data:image/png;base64,"


class UploadValidationError(ValueError):
    pass


def configured_upload_limit() -> int:
    raw_value = os.getenv("MAX_UPLOAD_BYTES")
    if raw_value is None:
        return DEFAULT_UPLOAD_LIMIT_BYTES
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("MAX_UPLOAD_BYTES must be an integer") from exc
    if value <= 0:
        raise RuntimeError("MAX_UPLOAD_BYTES must be greater than zero")
    return value


def validate_transfer_filename(filename: str | None) -> str:
    if not filename:
        raise UploadValidationError("A filename is required")
    if len(filename.encode("utf-8")) > 255:
        raise UploadValidationError("Filename is too long")
    if filename in {".", ".."} or PurePath(filename).name != filename:
        raise UploadValidationError("Filename must not contain a path")
    if any(character in filename for character in ("/", "\\", "|", "\0")):
        raise UploadValidationError("Filename contains unsupported characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise UploadValidationError("Filename contains control characters")
    return filename


async def read_upload_limited(upload: UploadFile, max_bytes: int) -> bytes:
    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise UploadValidationError(
            f"File exceeds the maximum size of {max_bytes} bytes"
        )
    return content


def resolve_local_file(root: Path, filename: str) -> Path:
    safe_filename = validate_transfer_filename(filename)
    try:
        resolved_root = root.resolve(strict=True)
        unresolved_candidate = resolved_root / safe_filename
        if unresolved_candidate.is_symlink():
            raise UploadValidationError("Collected file must not be a symlink")
        candidate = unresolved_candidate.resolve(strict=True)
        candidate.relative_to(resolved_root)
    except UploadValidationError:
        raise
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise UploadValidationError("Collected file was not found") from exc

    if not candidate.is_file():
        raise UploadValidationError("Collected file was not found")
    return candidate


def decode_png_image(value: str, max_bytes: int) -> bytes:
    """Decode a base64 or PNG data-URL snapshot, enforcing type and size."""
    if value.startswith("data:"):
        header, separator, payload = value.partition(",")
        if (
            not separator
            or header.lower() != PNG_DATA_URL_PREFIX[:-1]
            or not payload
        ):
            raise UploadValidationError("Snapshot must be a base64 PNG data URL")
        value = payload
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UploadValidationError("Snapshot is not valid base64") from exc
    if len(decoded) > max_bytes:
        raise UploadValidationError(
            f"Snapshot exceeds the maximum size of {max_bytes} bytes"
        )
    if not decoded.startswith(PNG_MAGIC):
        raise UploadValidationError("Snapshot is not a PNG image")
    return decoded
