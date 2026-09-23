"""Preprocessing variants and perspective correction.

No GPU, no OCR engine, no camera: these operate on synthetic arrays and assert
on shapes, invariants and configuration handling. What they cannot assert is
whether a variant helps ACCURACY — that needs ground truth and lives in
tools/anpr_bench.py.
"""
import cv2
import numpy as np
import pytest

from app.config import settings
from app.pipeline import plate_preprocess


def _plate_crop(width: int = 200, height: int = 60) -> np.ndarray:
    """A synthetic plate: dark glyph-like bars on a light ground."""
    crop = np.full((height, width, 3), 220, dtype=np.uint8)
    for index in range(6):
        x = 10 + index * 30
        cv2.rectangle(crop, (x, 15), (x + 18, height - 15), (20, 20, 20), -1)
    return crop


class TestVariants:
    def test_the_default_configuration_produces_exactly_one_image(self, monkeypatch):
        """The production default must cost ONE OCR pass. If this ever returns
        more, every camera's OCR bill silently multiplies."""
        monkeypatch.setattr(settings, "plate_preprocess_variants", "clahe")
        variants = plate_preprocess.build_variants(_plate_crop())
        assert len(variants) == 1
        assert variants[0][0] == "clahe"

    def test_every_named_variant_can_be_produced(self):
        variants = plate_preprocess.build_variants(
            _plate_crop(), variant_names=plate_preprocess.VARIANT_NAMES,
        )
        assert [name for name, _ in variants] == list(plate_preprocess.VARIANT_NAMES)
        for name, image in variants:
            assert image.size > 0, f"{name} produced an empty image"

    def test_the_original_variant_is_not_modified(self):
        crop = _plate_crop()
        (_, image), = plate_preprocess.build_variants(crop, variant_names=("original",))
        # Upscaling may resize it, but the content must not be transformed —
        # a 3-channel crop stays 3-channel.
        assert image.ndim == crop.ndim

    def test_thresholding_variants_are_binary(self):
        for name in ("adaptive", "otsu"):
            (_, image), = plate_preprocess.build_variants(_plate_crop(), variant_names=(name,))
            assert set(np.unique(image)).issubset({0, 255}), f"{name} is not binary"

    def test_an_empty_or_degenerate_crop_yields_no_variants(self):
        assert plate_preprocess.build_variants(None) == []
        assert plate_preprocess.build_variants(np.zeros((0, 0, 3), dtype=np.uint8)) == []

    def test_variants_are_independent_images(self):
        """A variant must not be a view onto another variant's buffer — an
        in-place OCR preprocessing step would otherwise corrupt its siblings."""
        variants = plate_preprocess.build_variants(
            _plate_crop(), variant_names=("gray", "clahe", "otsu"),
        )
        images = [image for _, image in variants]
        for i, first in enumerate(images):
            for second in images[i + 1:]:
                assert first is not second


class TestConfiguration:
    def test_unknown_variant_names_are_dropped_not_fatal(self, monkeypatch):
        """A typo in an env var must not take a camera worker down."""
        monkeypatch.setattr(settings, "plate_preprocess_variants", "clahe,nonsense,sharpen")
        assert plate_preprocess.configured_variants() == ("clahe", "sharpen")

    def test_an_empty_configuration_falls_back_to_the_default(self, monkeypatch):
        """OCR must always receive exactly one image, never zero."""
        monkeypatch.setattr(settings, "plate_preprocess_variants", "")
        monkeypatch.setattr(settings, "plate_preprocess_default_variant", "clahe")
        assert plate_preprocess.configured_variants() == ("clahe",)

    def test_an_entirely_invalid_configuration_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_preprocess_variants", "nonsense,garbage")
        monkeypatch.setattr(settings, "plate_preprocess_default_variant", "clahe")
        assert plate_preprocess.configured_variants() == ("clahe",)

    def test_whitespace_and_case_are_tolerated(self, monkeypatch):
        monkeypatch.setattr(settings, "plate_preprocess_variants", " CLAHE , Sharpen ")
        assert plate_preprocess.configured_variants() == ("clahe", "sharpen")


class TestUpscaling:
    def test_a_small_crop_is_upscaled_to_the_target_height(self):
        upscaled = plate_preprocess.upscale_for_ocr(_plate_crop(60, 18), target_height=64)
        assert upscaled.shape[0] == 64

    def test_aspect_ratio_is_preserved(self):
        crop = _plate_crop(120, 20)
        upscaled = plate_preprocess.upscale_for_ocr(crop, target_height=60)
        assert upscaled.shape[1] == pytest.approx(120 * 3, abs=2)

    def test_a_crop_already_large_enough_is_untouched(self):
        """Enlarging an already-readable crop costs time and adds no
        information."""
        crop = _plate_crop(400, 120)
        assert plate_preprocess.upscale_for_ocr(crop, target_height=64) is crop


class TestPerspectiveCorrection:
    def test_corners_are_ordered_clockwise_from_top_left(self):
        """boxPoints returns corners in a rotation-dependent order; without
        normalizing, some angles warp to a mirrored or rotated plate."""
        scrambled = np.array([[100, 50], [0, 50], [100, 0], [0, 0]], dtype=np.float32)
        ordered = plate_preprocess.order_quad(scrambled)
        assert list(ordered[0]) == [0, 0]
        assert list(ordered[1]) == [100, 0]
        assert list(ordered[2]) == [100, 50]
        assert list(ordered[3]) == [0, 50]

    def test_a_skewed_quad_warps_to_a_rectangle(self):
        crop = _plate_crop(200, 80)
        skewed = [[10, 5], [190, 20], [185, 70], [15, 60]]
        warped = plate_preprocess.four_point_transform(crop, skewed)
        assert warped is not None and warped.size > 0

    def test_a_degenerate_quad_returns_none_rather_than_a_smear(self):
        """Collinear points cannot describe a plate; the caller must keep the
        un-warped crop instead of handing OCR the result."""
        collinear = [[0, 0], [1, 0], [2, 0], [3, 0]]
        assert plate_preprocess.four_point_transform(_plate_crop(), collinear) is None

    def test_a_square_on_axis_quad_is_not_worth_correcting(self):
        """An axis-aligned box warps to approximately itself, so the transform
        only costs resampling blur."""
        assert plate_preprocess.needs_perspective_correction(
            [[0, 0], [100, 0], [100, 40], [0, 40]]
        ) is False

    def test_a_genuinely_skewed_quad_is_worth_correcting(self):
        assert plate_preprocess.needs_perspective_correction(
            [[0, 0], [100, 20], [95, 70], [5, 40]]
        ) is True

    def test_no_quad_means_no_correction(self):
        assert plate_preprocess.needs_perspective_correction(None) is False

    def test_a_malformed_quad_is_refused_not_raised(self):
        """A detector returning something unexpected must degrade the read, not
        crash a camera worker."""
        assert plate_preprocess.four_point_transform(_plate_crop(), [[0, 0], [1, 1]]) is None
        assert plate_preprocess.needs_perspective_correction("not a quad") is False

    def test_build_variants_accepts_a_quad_without_raising(self):
        variants = plate_preprocess.build_variants(
            _plate_crop(200, 80), quad=[[10, 5], [190, 20], [185, 70], [15, 60]],
            variant_names=("clahe",),
        )
        assert len(variants) == 1 and variants[0][1].size > 0
