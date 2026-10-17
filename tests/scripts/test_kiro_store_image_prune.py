"""Tests for scripts/kiro_store_image_prune.py (set5think fork custom tool).

Covers the pure JSON-pruning logic (keep most-recent N image blocks, replace
older ones with placeholders, both anthropic and mcp shapes) and an end-to-end
run against a temporary sqlite store mirroring kiro-cli's conversations_v2.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "kiro_store_image_prune.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kiro_store_image_prune", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _anthropic_img(tag):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": f"D{tag}"}}


def _mcp_img(tag):
    return {"type": "image", "data": f"M{tag}", "mimeType": "image/png"}


def test_prune_value_json_keeps_most_recent():
    mod = _load_module()
    turns = []
    for i in range(10):
        turns.append({"role": "tool", "content": [{"type": "text", "text": f"s{i}"}, _anthropic_img(i)]})
    value = json.dumps({"history": turns})
    new_json, before, after = mod.prune_value_json(value, keep=3)
    assert before == 10
    assert after == 3
    doc = json.loads(new_json)
    # Collect surviving image tags in order.
    survivors = []
    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "image":
                survivors.append(n["source"]["data"])
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(doc)
    assert survivors == ["D7", "D8", "D9"]  # most-recent 3


def test_prune_value_json_handles_mcp_shape():
    mod = _load_module()
    turns = [{"content": [_mcp_img(i)]} for i in range(5)]
    value = json.dumps({"history": turns})
    new_json, before, after = mod.prune_value_json(value, keep=2)
    assert before == 5
    assert after == 2


def test_prune_value_json_noop_under_budget():
    mod = _load_module()
    value = json.dumps({"history": [{"content": [_anthropic_img(0)]}]})
    new_json, before, after = mod.prune_value_json(value, keep=40)
    assert before == after == 1
    assert new_json == value


def test_prune_value_json_bad_json_unchanged():
    mod = _load_module()
    out, before, after = mod.prune_value_json("not json", keep=5)
    assert out == "not json" and before == 0 and after == 0


def test_end_to_end_sqlite_prune(tmp_path):
    db = tmp_path / "data.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE conversations_v2 (key TEXT, conversation_id TEXT, value TEXT, "
        "created_at INT, updated_at INT)"
    )
    turns = [{"content": [_anthropic_img(i)]} for i in range(101)]
    con.execute(
        "INSERT INTO conversations_v2 VALUES (?,?,?,?,?)",
        ("k", "conv_x", json.dumps({"history": turns}), 0, 0),
    )
    con.commit()
    con.close()

    # Dry-run first: should NOT modify.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--keep", "40"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(db)
    v = con.execute("SELECT value FROM conversations_v2 WHERE conversation_id='conv_x'").fetchone()[0]
    con.close()
    assert json.dumps(json.loads(v)).count('"type":"image"') + json.dumps(json.loads(v)).count('"type": "image"') >= 0
    # count images still 101 after dry run
    imgs = sum(1 for t in json.loads(v)["history"] for b in t["content"] if b.get("type") == "image")
    assert imgs == 101

    # Apply.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--keep", "40", "--apply"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(db)
    v = con.execute("SELECT value FROM conversations_v2 WHERE conversation_id='conv_x'").fetchone()[0]
    con.close()
    doc = json.loads(v)
    imgs = sum(1 for t in doc["history"] for b in t["content"] if b.get("type") == "image")
    phs = sum(
        1 for t in doc["history"] for b in t["content"]
        if b.get("type") == "text" and "omitted from stored history" in b.get("text", "")
    )
    assert imgs == 40
    assert phs == 61
    # A backup file was created alongside the db.
    assert list(tmp_path.glob("data.sqlite3.bak.*"))
