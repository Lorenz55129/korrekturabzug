"""Unit tests for DPI calculation logic."""

import pytest

from app.preflight.rules.image_dpi import compute_effective_dpi


class TestComputeEffectiveDPI:
    def test_standard_300dpi(self):
        """A 300x300 px image placed at 1x1 inch (72x72 pt) -> 300 DPI."""
        dpi_x, dpi_y = compute_effective_dpi(300, 300, 72.0, 72.0)
        assert dpi_x == 300.0
        assert dpi_y == 300.0

    def test_150dpi(self):
        """150x150 px at 1x1 inch -> 150 DPI."""
        dpi_x, dpi_y = compute_effective_dpi(150, 150, 72.0, 72.0)
        assert dpi_x == 150.0
        assert dpi_y == 150.0

    def test_scaled_up(self):
        """300x300 px placed at 2x2 inch (144x144 pt) -> 150 DPI."""
        dpi_x, dpi_y = compute_effective_dpi(300, 300, 144.0, 144.0)
        assert dpi_x == 150.0
        assert dpi_y == 150.0

    def test_non_square(self):
        """600x300 px at 2x1 inch (144x72 pt)."""
        dpi_x, dpi_y = compute_effective_dpi(600, 300, 144.0, 72.0)
        assert dpi_x == 300.0
        assert dpi_y == 300.0

    def test_asymmetric(self):
        """600x300 px at 1x1 inch -> 600 x 300 DPI."""
        dpi_x, dpi_y = compute_effective_dpi(600, 300, 72.0, 72.0)
        assert dpi_x == 600.0
        assert dpi_y == 300.0

    def test_zero_placed_size(self):
        """Zero placed size should not crash, returns 0."""
        dpi_x, dpi_y = compute_effective_dpi(300, 300, 0.0, 0.0)
        assert dpi_x == 0.0
        assert dpi_y == 0.0

    def test_very_large_image(self):
        """4000x3000 px at 10x7.5 inch placement."""
        dpi_x, dpi_y = compute_effective_dpi(4000, 3000, 720.0, 540.0)
        assert dpi_x == 400.0
        assert dpi_y == 400.0

    def test_small_thumbnail(self):
        """50x50 px at 1x1 inch -> 50 DPI (below any threshold)."""
        dpi_x, dpi_y = compute_effective_dpi(50, 50, 72.0, 72.0)
        assert dpi_x == 50.0
        assert dpi_y == 50.0
