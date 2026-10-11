"""Tests for the base-branch compare feature (set5think fork custom patch).

Covers GitFilesystemRegistry.list_branches / default_base and the
compare-to-base mode of list_changed_files (merge-base diff), using a real
git repo so the subprocess codepaths are fully exercised.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omnigent.runtime.filesystem_registry import GitFilesystemRegistry


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, env=env)


def _init_repo_with_feature_branch(tmp_path: Path) -> dict[str, str]:
    """Init a repo with a 'main' base and a 'feature' branch adding/editing files.

    Layout after setup (HEAD = feature):
      base.py       : committed on main (unchanged on feature)
      shared.py     : committed on main, MODIFIED on feature
      feature_new.py: ADDED on feature
    """
    env = _git_env()
    _run(["git", "init", "-b", "main"], tmp_path, env)
    (tmp_path / "base.py").write_text("base\n")
    (tmp_path / "shared.py").write_text("line1\nline2\n")
    _run(["git", "add", "-A"], tmp_path, env)
    _run(["git", "commit", "-m", "main init"], tmp_path, env)

    _run(["git", "checkout", "-b", "feature"], tmp_path, env)
    (tmp_path / "shared.py").write_text("line1\nline2 edited\nline3\n")
    (tmp_path / "feature_new.py").write_text("new on feature\n")
    _run(["git", "add", "-A"], tmp_path, env)
    _run(["git", "commit", "-m", "feature work"], tmp_path, env)
    return env


def test_list_branches_current_first(tmp_path: Path) -> None:
    _init_repo_with_feature_branch(tmp_path)
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    branches = reg.list_branches()
    assert set(branches) == {"main", "feature"}
    # Current branch (feature) must sort first.
    assert branches[0] == "feature"


def test_default_base_prefers_main(tmp_path: Path) -> None:
    _init_repo_with_feature_branch(tmp_path)
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    # No origin/HEAD in a bare local repo, so it falls back to the main/master
    # candidate list.
    assert reg.default_base() == "main"


def test_compare_to_base_lists_branch_changes(tmp_path: Path) -> None:
    """base_ref mode returns this branch's committed changes vs the merge-base."""
    _init_repo_with_feature_branch(tmp_path)
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    # Uncommitted view is empty (feature work is committed).
    assert reg.list_changed_files("conv", limit=100) == []

    # Compare-to-base shows the branch's own changes.
    records = reg.list_changed_files("conv", limit=100, base_ref="main")
    by_path = {r["path"]: r for r in records}
    assert set(by_path) == {"shared.py", "feature_new.py"}, by_path
    assert by_path["feature_new.py"]["status"] == "created"
    assert by_path["shared.py"]["status"] == "modified"
    # Numstat line counts flow through for the modified file.
    assert by_path["shared.py"]["lines_added"] is not None


def test_compare_to_base_baseline_is_merge_base_content(tmp_path: Path) -> None:
    """get_baseline(base_ref=...) returns the merge-base content, not HEAD."""
    _init_repo_with_feature_branch(tmp_path)
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)

    # Against main, the baseline for shared.py is the pre-edit content.
    assert reg.get_baseline("shared.py", base_ref="main") == "line1\nline2\n"
    # A file that only exists on feature has no baseline on main.
    assert reg.get_baseline("feature_new.py", base_ref="main") is None
    # Default (no base_ref) reads HEAD — the committed feature content.
    assert reg.get_baseline("shared.py") == "line1\nline2 edited\nline3\n"


def test_unknown_base_ref_returns_empty(tmp_path: Path) -> None:
    _init_repo_with_feature_branch(tmp_path)
    reg = GitFilesystemRegistry(watch_path=tmp_path, git_root=tmp_path)
    assert reg.list_changed_files("conv", limit=100, base_ref="does-not-exist") == []
