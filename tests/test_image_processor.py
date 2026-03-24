"""Tests for image processing helpers."""

from __future__ import annotations

import io

import pytest
from PIL import Image as PILImage

from mcp_local_reference.services.image_processor import ImageProcessor


@pytest.fixture()
def red_png() -> bytes:
    """A simple 200x100 red PNG."""
    img = PILImage.new("RGB", (200, 100), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCropImage:
    def test_crop_reduces_dimensions(self, red_png: bytes) -> None:
        cropped = ImageProcessor.crop_image(red_png, bbox=(10, 10, 100, 50))
        img = PILImage.open(io.BytesIO(cropped))
        assert img.width == 90
        assert img.height == 40

    def test_no_crop_preserves_original(self, red_png: bytes) -> None:
        out = ImageProcessor.crop_image(red_png)
        img = PILImage.open(io.BytesIO(out))
        assert img.width == 200
        assert img.height == 100


class TestBase64:
    def test_round_trip(self, red_png: bytes) -> None:
        b64 = ImageProcessor.to_base64(red_png)
        assert isinstance(b64, str)
        assert len(b64) > 0


class TestGetImageInfo:
    def test_returns_dimensions(self, red_png: bytes) -> None:
        info = ImageProcessor.get_image_info(red_png)
        assert info["width"] == 200
        assert info["height"] == 100
