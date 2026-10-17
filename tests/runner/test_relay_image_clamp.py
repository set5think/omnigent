"""Tests for the native-CLI relay image clamp (set5think fork custom patch).

Covers: oversized image downscaled under the per-image cap, surplus images in a
single result evicted to placeholders, small images and non-image content left
untouched, and malformed input returned unchanged (never raises).
"""

from __future__ import annotations

import base64
import io

import pytest

from omnigent.runner.relay_image_clamp import (
    _MAX_IMAGE_BASE64_BYTES,
    _MAX_IMAGES_PER_RESULT,
    clamp_relay_result_images,
)


def _big_png_base64(width: int = 3000, height: int = 3000) -> str:
    """Build a PNG whose base64 comfortably exceeds the per-image cap."""
    from PIL import Image

    # Random-ish noise defeats PNG compression so the payload is genuinely large.
    import os

    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _small_png_base64() -> str:
    from PIL import Image

    img = Image.new("RGB", (16, 16), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_oversized_anthropic_image_is_downscaled_under_cap():
    big = _big_png_base64()
    assert len(big) > _MAX_IMAGE_BASE64_BYTES  # precondition
    result = {
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}}
        ]
    }
    out = clamp_relay_result_images(result)
    block = out["content"][0]
    # Still an image (not evicted), now JPEG and under the cap.
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    assert len(block["source"]["data"]) <= _MAX_IMAGE_BASE64_BYTES
    # And it still decodes to a valid image.
    from PIL import Image

    Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))).verify()


def test_oversized_mcp_shape_image_is_downscaled():
    big = _big_png_base64()
    result = {"content": [{"type": "image", "data": big, "mimeType": "image/png"}]}
    out = clamp_relay_result_images(result)
    block = out["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/jpeg"
    assert len(block["data"]) <= _MAX_IMAGE_BASE64_BYTES


def test_small_image_passes_through_untouched():
    small = _small_png_base64()
    result = {
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": small}}
        ]
    }
    out = clamp_relay_result_images(result)
    assert out["content"][0]["source"]["data"] == small
    assert out["content"][0]["source"]["media_type"] == "image/png"


def test_text_blocks_untouched():
    result = {"content": [{"type": "text", "text": "hello"}]}
    out = clamp_relay_result_images(result)
    assert out == result


def test_surplus_images_evicted_to_placeholders():
    small = _small_png_base64()

    def img():
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": small}}

    n = _MAX_IMAGES_PER_RESULT + 2
    result = {"content": [img() for _ in range(n)]}
    out = clamp_relay_result_images(result)
    images = [b for b in out["content"] if isinstance(b, dict) and b.get("type") == "image"]
    placeholders = [
        b
        for b in out["content"]
        if isinstance(b, dict) and b.get("type") == "text" and "omitted from history" in b.get("text", "")
    ]
    assert len(images) == _MAX_IMAGES_PER_RESULT
    assert len(placeholders) == 2
    # The KEPT images are the most recent ones (last in the list).
    assert out["content"][-1]["type"] == "image"


@pytest.mark.parametrize("bad", [None, 42, "string", {"content": "not-a-list"}, {}, {"content": []}])
def test_malformed_input_returned_unchanged(bad):
    assert clamp_relay_result_images(bad) == bad


# ---------------------------------------------------------------------------
# Per-session cumulative image-count budget (IMAGE_COUNT_EXCEEDED defense).
# ---------------------------------------------------------------------------

import uuid

from omnigent.runner.relay_image_clamp import (
    _MAX_SESSION_IMAGES,
    reset_session_image_count,
    session_image_count,
)


def _img_block():
    """A small, under-cap image block (so size/downscale logic is a no-op)."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _small_png_base64()},
    }


def _fresh_session() -> str:
    sid = f"test-{uuid.uuid4().hex}"
    reset_session_image_count(sid)
    return sid


def _count_images(content):
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")


def _count_budget_placeholders(content):
    return sum(
        1
        for b in content
        if isinstance(b, dict)
        and b.get("type") == "text"
        and "per-session limit" in b.get("text", "")
    )


def test_session_budget_passes_images_under_limit():
    sid = _fresh_session()
    # Deliver a handful of single-image results; all should pass as real images.
    for _ in range(5):
        out = clamp_relay_result_images({"content": [_img_block()]}, sid)
        assert _count_images(out["content"]) == 1
        assert _count_budget_placeholders(out["content"]) == 0
    assert session_image_count(sid) == 5
    reset_session_image_count(sid)


def test_session_budget_placeholders_images_over_limit():
    sid = _fresh_session()
    delivered_real = 0
    delivered_placeholder = 0
    # Push well past the budget, one image per result (the real-world shape).
    for _ in range(_MAX_SESSION_IMAGES + 20):
        out = clamp_relay_result_images({"content": [_img_block()]}, sid)
        delivered_real += _count_images(out["content"])
        delivered_placeholder += _count_budget_placeholders(out["content"])
    # Exactly the budget's worth of real images ever get through; the rest are
    # budget placeholders. This is what keeps kiro's stored media-segment count
    # from ever reaching the backend's 100 cap.
    assert delivered_real == _MAX_SESSION_IMAGES
    assert delivered_placeholder == 20
    assert session_image_count(sid) == _MAX_SESSION_IMAGES
    reset_session_image_count(sid)


def test_session_budget_partial_result_at_boundary():
    """A multi-image result straddling the budget keeps the most-recent ones."""
    sid = _fresh_session()
    # Spend all but 2 of the budget.
    for _ in range(_MAX_SESSION_IMAGES - 2):
        clamp_relay_result_images({"content": [_img_block()]}, sid)
    assert session_image_count(sid) == _MAX_SESSION_IMAGES - 2
    # Now a result with 5 images: only 2 fit the budget; they must be the LAST 2.
    content = [_img_block() for _ in range(5)]
    # tag each image so we can tell which survived
    for i, b in enumerate(content):
        b["source"]["data"] = _small_png_base64()
        b["_tag"] = i
    out = clamp_relay_result_images({"content": content}, sid)
    kept = [b for b in out["content"] if isinstance(b, dict) and b.get("type") == "image"]
    # Only 2 images survive (the session had 2 of its budget left). They must be
    # the most recent. NOTE: the per-RESULT cap (_MAX_IMAGES_PER_RESULT) trims the
    # 5-image result to its 2 newest FIRST, and those 2 then fit the remaining
    # budget — so the eviction here is attributable to the per-result rule, with
    # the session budget landing exactly at zero remaining. Either way the net is
    # 2 real images kept + 3 placeholders, and the kept ones are the newest.
    assert len(kept) == 2
    assert sorted(b["_tag"] for b in kept) == [3, 4]
    placeholders = [
        b for b in out["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert len(placeholders) == 3
    assert session_image_count(sid) == _MAX_SESSION_IMAGES
    reset_session_image_count(sid)


def test_session_budget_isolated_per_session():
    a = _fresh_session()
    b = _fresh_session()
    for _ in range(_MAX_SESSION_IMAGES):
        clamp_relay_result_images({"content": [_img_block()]}, a)
    # Session a is now at budget; session b is untouched.
    assert session_image_count(a) == _MAX_SESSION_IMAGES
    assert session_image_count(b) == 0
    out_a = clamp_relay_result_images({"content": [_img_block()]}, a)
    out_b = clamp_relay_result_images({"content": [_img_block()]}, b)
    assert _count_images(out_a["content"]) == 0  # over budget -> placeholder
    assert _count_budget_placeholders(out_a["content"]) == 1
    assert _count_images(out_b["content"]) == 1  # fresh session -> real
    reset_session_image_count(a)
    reset_session_image_count(b)


def test_reset_session_image_count_clears_budget():
    sid = _fresh_session()
    for _ in range(_MAX_SESSION_IMAGES):
        clamp_relay_result_images({"content": [_img_block()]}, sid)
    assert session_image_count(sid) == _MAX_SESSION_IMAGES
    reset_session_image_count(sid)
    assert session_image_count(sid) == 0
    # After reset the session gets a fresh budget.
    out = clamp_relay_result_images({"content": [_img_block()]}, sid)
    assert _count_images(out["content"]) == 1
    reset_session_image_count(sid)


def test_no_session_id_skips_cumulative_budget():
    """Backward-compat: no session_id → only per-image/per-result bounds apply."""
    # Many single-image results without a session id are all delivered real
    # (the cumulative budget simply isn't tracked).
    for _ in range(_MAX_SESSION_IMAGES + 10):
        out = clamp_relay_result_images({"content": [_img_block()]})
        assert _count_images(out["content"]) == 1
        assert _count_budget_placeholders(out["content"]) == 0
