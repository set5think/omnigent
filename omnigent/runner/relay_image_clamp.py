"""Clamp inline image payloads on the native-CLI tool-result relay path.

CUSTOM PATCH (set5think fork) — see the ``omnigent-fork`` skill's customization
manifest. Not present upstream as of base tag v0.15.0.

Why this exists
---------------
Native-CLI harnesses (Kiro and siblings) call Omnigent MCP tools through the
``serve-mcp`` relay. The relay returns tool results — including full-resolution
base64 image blocks from tools like ``browser_screenshot`` or headless
screenshots — verbatim to the CLI. The CLI (kiro-cli) stores those images in
its OWN conversation history (``data.sqlite3``) and replays the entire image
history to its backend (CodeWhisperer ``GenerateAssistantResponse``) on every
turn. That backend enforces several limits on the replayed request.

Omnigent does not control kiro-cli's retained history, so the lever we DO have
is the only reliable fix: control what we hand the CLI. This module enforces
THREE bounds, each defending a different backend limit:

* **Per-image size** — **downscales/recompresses** any image block whose base64
  exceeds a safe cap (``_MAX_IMAGE_BASE64_BYTES``, below the backend's 5 MB)
  until it fits, preserving a usable (smaller) image for vision. Defends the
  ``IMAGE_SIZE_EXCEEDED`` / "image exceeds 5 MB maximum" error.
* **Per-result count** — **placeholder-evicts** all but the most recent
  ``_MAX_IMAGES_PER_RESULT`` image blocks within a single tool result, so one
  call returning many images can't itself blow the body.
* **Per-session cumulative count** — the backend ALSO hard-caps the TOTAL number
  of inline media segments across the whole replayed history
  (``ValidationException: "too many inline media segments: 101 exceeds limit
  100"``, reason ``IMAGE_COUNT_EXCEEDED``). Because kiro-cli replays its entire
  stored image history every turn, the N-th screenshot of a long session trips
  this even when every individual image is small. The relay is invoked once per
  tool call and cannot reach kiro's already-stored history, so the only lever is
  to BOUND how many real images we ever hand out per session: this module keeps
  a per-session counter (keyed by ``session_id``) and, once a session has been
  given ``_MAX_SESSION_IMAGES`` real images, delivers every FURTHER image as a
  lightweight text placeholder instead. A placeholder is NOT an inline media
  segment, so the count of media segments kiro stores plateaus safely under the
  backend cap and the session can never wedge on ``IMAGE_COUNT_EXCEEDED``. The
  placeholder text also nudges the agent toward text-based state inspection
  rather than yet another screenshot.

It walks both the Anthropic-style ``{"type":"image","source":{"type":"base64",
...}}`` shape and the MCP ``{"type":"image","data":...,"mimeType":...}`` shape.
Non-image content passes through unchanged.

Thread-safety: the per-session counter is guarded by a module lock. The relay
executor is created per session and invoked serially per turn, but turns across
sessions can overlap, so the shared registry is locked on every mutation.
"""

from __future__ import annotations

import base64
import binascii
import io
import threading
from typing import Any

# Backend hard cap is 5 MB (5_242_880) of base64 per image; stay well under so
# the base64 expansion and JSON framing have headroom.
_MAX_IMAGE_BASE64_BYTES = 4_000_000
# Keep at most this many images per single tool result; older ones in the same
# result become text placeholders. One screenshot per call is typical, so this
# only bites pathological multi-image results.
_MAX_IMAGES_PER_RESULT = 2
# Downscale search floor: never shrink the longest edge below this (keeps the
# image legible rather than producing a useless thumbnail).
_MIN_LONGEST_EDGE = 480
# Backend hard cap on TOTAL inline media segments across the replayed history is
# 100 (reason IMAGE_COUNT_EXCEEDED). We bound the real images WE hand a session
# well under that, leaving headroom for user-uploaded images and any media the
# CLI adds on its own. Once a session has received this many real images from
# the relay, every further image is delivered as a text placeholder so the
# stored media-segment count can never reach the backend cap.
_MAX_SESSION_IMAGES = 60

# Per-session count of real images handed to the CLI so far, keyed by
# session_id. Guarded by _SESSION_LOCK. A process restart resets it, which is
# safe: a fresh process means a fresh (or compacted) conversation replay.
_SESSION_IMAGE_COUNTS: dict[str, int] = {}
_SESSION_LOCK = threading.Lock()


def _placeholder_block(media_type: str | None) -> dict[str, Any]:
    """Return the text block that replaces an evicted/undecodable image."""
    label = f"{media_type} image" if media_type else "image"
    return {
        "type": "text",
        "text": (
            f"[{label} omitted from history to keep the request under the "
            "backend image-size limit — re-run the tool call above to view it again]"
        ),
    }


def _budget_placeholder_block(media_type: str | None, count: int) -> dict[str, Any]:
    """Placeholder for an image suppressed because the session image budget is spent.

    Distinct text from the size/per-result placeholder: it tells the agent the
    budget reason and steers it toward cheaper text-based state inspection,
    which is the real fix for screenshot-heavy sessions.
    """
    label = f"{media_type} image" if media_type else "image"
    return {
        "type": "text",
        "text": (
            f"[{label} omitted: this session has already captured {count} images, "
            f"the per-session limit of {_MAX_SESSION_IMAGES} that keeps the request "
            "under the backend's 100-inline-media-segment cap. Further screenshots "
            "will keep being omitted. Prefer text-based state inspection (query "
            "state directly, read logs/values, use accessibility/DOM snapshots) over "
            "new screenshots; if you truly need to see one, re-run a single specific "
            "capture — it will still be omitted from stored history but you can rely "
            "on the textual signals above.]"
        ),
    }


def _image_source_fields(block: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Extract (base64_data, media_type, shape) from an image block.

    ``shape`` is ``"anthropic"`` (data under ``source``), ``"mcp"`` (data at the
    top level), or ``""`` when the block is not a recognizable base64 image.
    """
    if block.get("type") != "image":
        return None, None, ""
    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data")
        if isinstance(data, str) and data:
            media_type = source.get("media_type")
            return data, media_type if isinstance(media_type, str) else None, "anthropic"
    data = block.get("data")
    if isinstance(data, str) and data:
        media_type = block.get("mimeType") or block.get("media_type")
        return data, media_type if isinstance(media_type, str) else None, "mcp"
    return None, None, ""


def _downscale_base64(data_b64: str, media_type: str | None) -> str | None:
    """Return a smaller base64 image under the cap, or ``None`` if infeasible.

    Iteratively reduces the longest edge (and, as a fallback, JPEG quality)
    until the re-encoded base64 fits ``_MAX_IMAGE_BASE64_BYTES``. Returns
    ``None`` when Pillow is unavailable, the data won't decode, or no size
    within the floor fits (caller then placeholder-evicts).
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow missing → caller evicts instead
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001 — undecodable image → caller evicts
        return None

    # PNG screenshots dominate; converting to JPEG shrinks dramatically. Keep
    # alpha-less output; flatten RGBA/P onto white so JPEG is valid.
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        try:
            mask = img.convert("RGBA").split()[-1]
            background.paste(img.convert("RGB"), mask=mask)
        except Exception:  # noqa: BLE001
            background = img.convert("RGB")
        img = background

    longest = max(img.size)
    quality = 85
    # Try progressively smaller dimensions; within each, drop quality a bit.
    while True:
        scale = min(1.0, longest / max(img.size)) if max(img.size) else 1.0
        if scale < 1.0:
            new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
            candidate = img.resize(new_size, Image.LANCZOS)
        else:
            candidate = img
        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        if len(encoded) <= _MAX_IMAGE_BASE64_BYTES:
            return encoded
        if quality > 55:
            quality -= 15
            continue
        quality = 85
        longest = int(longest * 0.8)
        if longest < _MIN_LONGEST_EDGE:
            return None


def _rewrite_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """Downscale an oversized image block in place-ish; evict if infeasible."""
    data_b64, media_type, shape = _image_source_fields(block)
    if not data_b64 or not shape:
        return block
    if len(data_b64) <= _MAX_IMAGE_BASE64_BYTES:
        return block
    shrunk = _downscale_base64(data_b64, media_type)
    if shrunk is None:
        return _placeholder_block(media_type)
    if shape == "anthropic":
        new_source = dict(block.get("source", {}))
        new_source["data"] = shrunk
        new_source["media_type"] = "image/jpeg"
        return {**block, "source": new_source}
    # mcp shape
    return {**block, "data": shrunk, "mimeType": "image/jpeg"}


def _clamp_content_list(content: list[Any]) -> list[Any]:
    """Clamp a list of MCP content blocks: downscale, then evict older images."""
    # First pass: downscale oversized images (and evict undecodable ones).
    processed: list[Any] = []
    image_positions: list[int] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image":
            rewritten = _rewrite_image_block(item)
            processed.append(rewritten)
            if isinstance(rewritten, dict) and rewritten.get("type") == "image":
                image_positions.append(len(processed) - 1)
        else:
            processed.append(item)
    # Second pass: keep only the most recent _MAX_IMAGES_PER_RESULT images.
    if len(image_positions) > _MAX_IMAGES_PER_RESULT:
        evict = set(image_positions[: len(image_positions) - _MAX_IMAGES_PER_RESULT])
        for idx in evict:
            block = processed[idx]
            media_type = None
            if isinstance(block, dict):
                _d, media_type, _s = _image_source_fields(block)
            processed[idx] = _placeholder_block(media_type)
    return processed


def _apply_session_image_budget(content: list[Any], session_id: str | None) -> list[Any]:
    """Bound cumulative real images per session; placeholder once over budget.

    Counts image blocks remaining in ``content`` (after per-image/per-result
    clamping) and compares against the session's running total. Images that
    would push the session over ``_MAX_SESSION_IMAGES`` are replaced with a
    budget placeholder. No session id → no cumulative tracking (returns as-is);
    the per-image/per-result bounds still applied upstream.
    """
    if not session_id:
        return content
    # Count how many real images are in this (already size/per-result clamped)
    # result, then reserve that many against the session budget atomically.
    image_idxs = [
        i
        for i, b in enumerate(content)
        if isinstance(b, dict) and b.get("type") == "image"
    ]
    if not image_idxs:
        return content
    with _SESSION_LOCK:
        already = _SESSION_IMAGE_COUNTS.get(session_id, 0)
        allowed = max(0, _MAX_SESSION_IMAGES - already)
        kept = min(allowed, len(image_idxs))
        _SESSION_IMAGE_COUNTS[session_id] = already + kept
    if kept >= len(image_idxs):
        return content  # whole result fits in budget
    # Keep the LAST ``kept`` images real (most recent/relevant), placeholder the
    # earlier ones in this result.
    evict = set(image_idxs[: len(image_idxs) - kept])
    out = list(content)
    running = already
    for i in image_idxs:
        if i in evict:
            media_type = None
            blk = out[i]
            if isinstance(blk, dict):
                _d, media_type, _s = _image_source_fields(blk)
            running += 1
            out[i] = _budget_placeholder_block(media_type, running)
    return out


def reset_session_image_count(session_id: str) -> None:
    """Forget a session's cumulative image count (e.g. on session end/compaction)."""
    if not session_id:
        return
    with _SESSION_LOCK:
        _SESSION_IMAGE_COUNTS.pop(session_id, None)


def session_image_count(session_id: str) -> int:
    """Return the real images handed to ``session_id`` so far (test/introspection)."""
    with _SESSION_LOCK:
        return _SESSION_IMAGE_COUNTS.get(session_id, 0)


def clamp_relay_result_images(result: Any, session_id: str | None = None) -> Any:
    """Clamp image payloads in a native-CLI relay tool result.

    Accepts the parsed MCP tool-call result object (typically
    ``{"content": [...]}``) and returns the same structure with: oversized
    images downscaled under the per-image cap; surplus images in a single
    result evicted to text placeholders; and — when ``session_id`` is given —
    images beyond the per-session cumulative budget (``_MAX_SESSION_IMAGES``)
    replaced with budget placeholders so the session can never trip the
    backend's inline-media-segment count cap. Any non-dict / content-less input
    is returned unchanged. Never raises — on any unexpected shape it returns the
    input as-is so a transform bug can't break tool delivery.
    """
    try:
        if not isinstance(result, dict):
            return result
        content = result.get("content")
        if isinstance(content, list):
            clamped = _clamp_content_list(content)
            clamped = _apply_session_image_budget(clamped, session_id)
            return {**result, "content": clamped}
        return result
    except Exception:  # noqa: BLE001 — never let clamping break tool delivery
        return result
