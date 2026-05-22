"""Image extraction and encoding for vision LLM analysis."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from PIL import Image

logger = get_logger(__name__)

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
# Map Pillow format names to MIME types
FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _load_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image
        from PIL.Image import Resampling
    except ModuleNotFoundError as exc:
        msg = "Pillow is required for image extraction. Install the `attachment` extra."
        raise ValueError(msg) from exc
    return Image, Resampling


@dataclass(frozen=True)
class ImageContent:
    """Extracted and encoded image content ready for vision LLM."""

    data_uri: str
    mime_type: str
    width: int
    height: int
    file_size_bytes: int


class ImageExtractor:
    """Stateless utility for extracting and encoding images for vision LLM input."""

    @staticmethod
    def extract(file_path: str | Path, *, max_dimension: int = 2048) -> ImageContent:
        """Open an image, validate, resize if needed, and return base64-encoded content.

        Args:
            file_path: Path to the image file.
            max_dimension: Maximum width or height before resizing (preserves aspect ratio).

        Returns:
            ImageContent with base64 data URI and metadata.

        Raises:
            ValueError: If the image format is unsupported or file is invalid.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            msg = f"Image file not found: {file_path}"
            raise ValueError(msg)

        pillow_image, resampling = _load_pillow()
        try:
            img: Image.Image = pillow_image.open(file_path)
        except Exception as exc:
            msg = f"Cannot open image file: {exc}"
            raise ValueError(msg) from exc

        fmt = img.format
        if fmt not in SUPPORTED_FORMATS:
            msg = f"Unsupported image format: {fmt}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
            raise ValueError(msg)

        # Resize if either dimension exceeds max
        width, height = img.size
        if width > max_dimension or height > max_dimension:
            ratio = min(max_dimension / width, max_dimension / height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            img = img.resize((new_width, new_height), resampling.LANCZOS)
            width, height = new_width, new_height
            logger.debug(
                "image_resized",
                extra={
                    "original_size": f"{img.size}",
                    "new_size": f"{width}x{height}",
                    "file": str(file_path),
                },
            )

        # Convert to JPEG for consistent size (unless PNG with transparency)
        output_format = "JPEG"
        mime_type = "image/jpeg"
        if fmt == "PNG" and img.mode in ("RGBA", "LA", "PA"):
            output_format = "PNG"
            mime_type = "image/png"
        elif img.mode in ("RGBA", "LA", "PA") or img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        save_kwargs = {}
        if output_format == "JPEG":
            save_kwargs["quality"] = 85
        img.save(buf, format=output_format, **save_kwargs)

        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"
        file_size = buf.tell()

        return ImageContent(
            data_uri=data_uri,
            mime_type=mime_type,
            width=width,
            height=height,
            file_size_bytes=file_size,
        )

    @staticmethod
    def extract_from_bytes(
        data: bytes, *, mime_hint: str = "image/jpeg", max_dimension: int = 2048
    ) -> ImageContent:
        """Extract image content from raw bytes (e.g., rendered PDF page).

        Args:
            data: Raw image bytes.
            mime_hint: Expected MIME type hint.
            max_dimension: Maximum dimension before resizing.

        Returns:
            ImageContent with base64 data URI and metadata.
        """
        pillow_image, resampling = _load_pillow()
        try:
            img: Image.Image = pillow_image.open(io.BytesIO(data))
        except Exception as exc:
            msg = f"Cannot open image from bytes: {exc}"
            raise ValueError(msg) from exc

        width, height = img.size
        if width > max_dimension or height > max_dimension:
            ratio = min(max_dimension / width, max_dimension / height)
            new_width = int(width * ratio)
            new_height = int(height * ratio)
            img = img.resize((new_width, new_height), resampling.LANCZOS)
            width, height = new_width, new_height

        if img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        mime_type = "image/jpeg"
        data_uri = f"data:{mime_type};base64,{encoded}"

        return ImageContent(
            data_uri=data_uri,
            mime_type=mime_type,
            width=width,
            height=height,
            file_size_bytes=buf.tell(),
        )
