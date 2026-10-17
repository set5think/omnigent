#!/usr/bin/env python3
"""Prune accumulated inline images from a kiro-cli conversation store.

CUSTOM TOOL (set5think fork) — companion to ``omnigent/runner/relay_image_clamp.py``.
See the ``omnigent-fork`` skill customization manifest.

Why
---
kiro-cli stores every tool-result image it is handed in its OWN conversation
history (``~/.local/share/kiro-cli/data.sqlite3``, table ``conversations_v2``,
column ``value`` = a JSON blob per conversation) and replays the whole image
history to its CodeWhisperer backend every turn. The backend hard-caps the
number of inline media segments in one request at **100**; once a conversation
accumulates 101+ images it fails EVERY turn with::

    ValidationException: too many inline media segments: 101 exceeds limit 100
    reason: IMAGE_COUNT_EXCEEDED

and it cannot self-heal — compaction itself needs a successful backend call.

The relay-side clamp (``relay_image_clamp.py``) PREVENTS a live session from
ever reaching the cap by budgeting the real images it hands out. This script is
the RECOVERY / maintenance complement: it operates on the PERSISTED store (a
conversation that was saved while wedged, or proactive hygiene before resuming
a screenshot-heavy conversation) and collapses all but the most recent
``--keep`` image blocks in a conversation's stored JSON into lightweight text
placeholders. A placeholder is not an inline media segment, so the replayed
media count drops back under the cap and the conversation can be resumed.

Scope / limits
--------------
* This touches the PERSISTED sqlite store only. It CANNOT heal a conversation
  whose history is still live in a running kiro-cli process's memory (kiro
  holds history in RAM during a session and only writes the store on save/exit)
  — stop/exit that kiro session first, then run this on the saved row.
* It is image-shape-agnostic: it recursively finds any dict with
  ``{"type": "image", ...}`` in either the Anthropic ``source.data`` shape or
  the MCP top-level ``data`` shape (the same shapes the relay clamp handles) and
  rewrites all but the most recent ``--keep`` to a text placeholder, in
  document order.
* Defaults to a DRY RUN. Pass ``--apply`` to write. Always takes a timestamped
  backup of the sqlite file before writing.

Usage
-----
    # Report image counts per conversation in the default store:
    python kiro_store_image_prune.py --report

    # Dry-run prune every conversation over the budget (keep most recent 40):
    python kiro_store_image_prune.py --keep 40

    # Apply to a single conversation:
    python kiro_store_image_prune.py --keep 40 --conversation <id> --apply

    # Point at a different user's store:
    python kiro_store_image_prune.py --db /home/mom/.local/share/kiro-cli/data.sqlite3 --report
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any

DEFAULT_DB = os.path.expanduser("~/.local/share/kiro-cli/data.sqlite3")
# Keep comfortably under the backend's 100-inline-media-segment cap by default.
DEFAULT_KEEP = 40

_PLACEHOLDER_TEXT = (
    "[image omitted from stored history by kiro_store_image_prune to keep the "
    "conversation under the backend's 100-inline-media-segment cap]"
)


def _is_image_block(obj: Any) -> bool:
    """True for a recognizable base64 image block (anthropic or mcp shape)."""
    if not isinstance(obj, dict) or obj.get("type") != "image":
        return False
    src = obj.get("source")
    if isinstance(src, dict) and isinstance(src.get("data"), str) and src.get("data"):
        return True
    if isinstance(obj.get("data"), str) and obj.get("data"):
        return True
    return False


def _collect_image_blocks(node: Any, out: list[dict[str, Any]]) -> None:
    """Depth-first collect image-block dicts in document order."""
    if isinstance(node, dict):
        if _is_image_block(node):
            out.append(node)
            return  # don't descend into an image block's own fields
        for v in node.values():
            _collect_image_blocks(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_image_blocks(v, out)


def _rewrite_image_blocks_to_placeholder(node: Any, evict_ids: set[int]) -> Any:
    """Return a copy of ``node`` with image blocks whose id() is in ``evict_ids``
    replaced by a text placeholder block."""
    if isinstance(node, dict):
        if _is_image_block(node) and id(node) in evict_ids:
            return {"type": "text", "text": _PLACEHOLDER_TEXT}
        return {k: _rewrite_image_blocks_to_placeholder(v, evict_ids) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_image_blocks_to_placeholder(v, evict_ids) for v in node]
    return node


def prune_value_json(value_json: str, keep: int) -> tuple[str, int, int]:
    """Prune a single conversation ``value`` JSON string.

    Returns (new_json, images_before, images_after). If the JSON doesn't parse
    or has <= keep images, returns the input unchanged.
    """
    try:
        doc = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return value_json, 0, 0
    images: list[dict[str, Any]] = []
    _collect_image_blocks(doc, images)
    before = len(images)
    if before <= keep:
        return value_json, before, before
    # Evict all but the most recent ``keep`` (last in document order).
    evict = {id(b) for b in images[: before - keep]}
    new_doc = _rewrite_image_blocks_to_placeholder(doc, evict)
    # Verify.
    check: list[dict[str, Any]] = []
    _collect_image_blocks(new_doc, check)
    return json.dumps(new_doc, separators=(",", ":")), before, len(check)


def _iter_conversations(con: sqlite3.Connection, conversation: str | None):
    cur = con.cursor()
    if conversation:
        cur.execute(
            "SELECT conversation_id, value FROM conversations_v2 WHERE conversation_id = ?",
            (conversation,),
        )
    else:
        cur.execute("SELECT conversation_id, value FROM conversations_v2")
    return cur.fetchall()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"kiro-cli store (default {DEFAULT_DB})")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help=f"most-recent images to keep per conversation (default {DEFAULT_KEEP})")
    ap.add_argument("--conversation", default=None, help="limit to one conversation_id")
    ap.add_argument("--report", action="store_true", help="only report image counts, no changes")
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"store not found: {args.db}", file=sys.stderr)
        return 2
    if args.keep < 0:
        print("--keep must be >= 0", file=sys.stderr)
        return 2

    con = sqlite3.connect(args.db)
    try:
        try:
            rows = _iter_conversations(con, args.conversation)
        except sqlite3.OperationalError as e:
            print(f"cannot read conversations_v2: {e}", file=sys.stderr)
            return 2

        if not rows:
            print("no conversations found (store empty or conversation_id not present)")
            return 0

        over_budget = []
        for cid, value in rows:
            if not isinstance(value, str):
                continue
            images: list[dict[str, Any]] = []
            try:
                _collect_image_blocks(json.loads(value), images)
            except (json.JSONDecodeError, TypeError):
                continue
            n = len(images)
            flag = "  <-- OVER" if n > args.keep else ""
            print(f"{cid}: {n} images{flag}")
            if n > args.keep:
                over_budget.append(cid)

        if args.report:
            return 0
        if not over_budget:
            print(f"\nnothing over the keep={args.keep} budget; no changes needed")
            return 0

        if args.apply:
            backup = f"{args.db}.bak.{int(time.time())}"
            shutil.copy2(args.db, backup)
            print(f"\nbackup written: {backup}")

        changed = 0
        for cid, value in rows:
            if cid not in over_budget or not isinstance(value, str):
                continue
            new_json, before, after = prune_value_json(value, args.keep)
            if new_json == value:
                continue
            print(f"{cid}: {before} -> {after} images "
                  f"({'APPLIED' if args.apply else 'dry-run, would apply'})")
            if args.apply:
                con.execute(
                    "UPDATE conversations_v2 SET value = ?, updated_at = ? "
                    "WHERE conversation_id = ?",
                    (new_json, int(time.time()), cid),
                )
                changed += 1
        if args.apply:
            con.commit()
            print(f"\ncommitted {changed} conversation(s)")
        else:
            print("\ndry run — re-run with --apply to write")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
