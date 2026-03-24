"""Image cropping and encoding helpers using Pillow."""

from __future__ import annotations

import base64
import io

from PIL import Image


class ImageProcessor:
    """Stateless helpers for cropping images and encoding them as base64."""

    @staticmethod
    def crop_image(
        image_bytes: bytes,
        bbox: tuple[int, int, int, int] | None = None,
    ) -> bytes:
        """Crop *image_bytes* to *bbox* (left, top, right, bottom) and return PNG."""
        img = Image.open(io.BytesIO(image_bytes))
        if bbox:
            img = img.crop(bbox)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def to_base64(image_bytes: bytes) -> str:
        return base64.b64encode(image_bytes).decode("utf-8")

    @staticmethod
    def get_image_info(image_bytes: bytes) -> dict[str, object]:
        img = Image.open(io.BytesIO(image_bytes))
        return {
            "width": img.width,
            "height": img.height,
            "format": img.format or "unknown",
            "mode": img.mode,
        }
