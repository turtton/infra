"""
Git/PR operations for skill-gitops.

Called from __init__.py's debounced flush with one or more dirty skill names.
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Git identity (config-ops convention) ────────────────────────────────────
GIT_NAME = "github-actions[bot]"
GIT_EMAIL = "github-actions[bot]@users.noreply.github.com"


def sync_changed_skills(
    *,
    skills: set[str],
    skills_dir: Path,
    infra_skills_dir: Path,
    infra_repo: Path,
    lock_file: Path,
) -> None:
    """Check each dirty skill, copy changes to infra repo, create a single PR.

    Acquires *lock_file* to prevent concurrent syncs.  Safe to call from a
    background thread — all I/O is local or via subprocess.
    """
    # ── Lock ────────────────────────────────────────────────────────────
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        logger.info("skill-gitops: lock held by another process — skipping")
        return
    try:
        _do_sync(skills, skills_dir, infra_skills_dir, infra_repo)
    finally:
        os.close(lock_fd)
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


# ── Internal ────────────────────────────────────────────────────────────────


def _do_sync(
    skills: set[str],
    skills_dir: Path,
    infra_skills_dir: Path,
    infra_repo: Path,
) -> None:
    """Core sync logic — one PR per batch call."""
    if not infra_repo.is_dir():
        logger.warning("skill-gitops: infra repo %s not found", infra_repo)
        return

    # 1. Verify gh CLI
    if _run(["gh", "auth", "status"], check=False).returncode != 0:
        logger.warning("skill-gitops: gh not authenticated — skipping")
        return

    # 2. Fetch latest main
    if _run(["git", "fetch", "origin", "main"], cwd=infra_repo).returncode != 0:
        logger.warning("skill-gitops: git fetch origin main failed")
        return

    # 3. Find skills with actual changes
    changed = _find_changed(skills, skills_dir, infra_skills_dir)
    if not changed:
        logger.info("skill-gitops: no actual content changes detected")
        return

    # 4. Create branch + worktree
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"hermes/skill-update-{timestamp}"
    worktree_path = Path(tempfile.gettempdir()) / f"infra-{branch.replace('/', '-')}"

    # ── Create branch from origin/main ────────────────────────────────
    if _run(["git", "branch", branch, "origin/main"], cwd=infra_repo).returncode != 0:
        logger.warning("skill-gitops: branch creation failed")
        return

    # ── Worktree add ──────────────────────────────────────────────────
    try:
        r = _run(
            ["git", "worktree", "add", str(worktree_path), branch],
            cwd=infra_repo,
        )
        if r.returncode != 0:
            _run(["git", "branch", "-D", branch], cwd=infra_repo, check=False)
            logger.warning("skill-gitops: worktree add failed")
            return
    except Exception:
        _run(["git", "branch", "-D", branch], cwd=infra_repo, check=False)
        raise

    # 5. Copy changed skill files into worktree
    summary_lines: list[str] = []
    for name in changed:
        src = skills_dir / name
        dst = worktree_path / infra_skills_dir.relative_to(infra_repo) / name
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        _sync_skill_dir(src, dst)
        summary_lines.append(f"- `{name}`")
        logger.info("skill-gitops: synced %s → %s", src, dst)

    if not summary_lines:
        logger.info("skill-gitops: nothing to copy after second check")
        _cleanup_worktree(infra_repo, branch, worktree_path)
        return

    # 6. Commit
    rc = _run(
        ["git", "add", "-A"],
        cwd=worktree_path,
    ).returncode
    if rc != 0:
        _cleanup_worktree(infra_repo, branch, worktree_path)
        return

    skill_list = ", ".join(sorted(changed))
    commit_msg = f"hermes: sync skill update — {skill_list}"
    rc = _run(
        ["git", "commit", "-m", commit_msg, "--author", f"{GIT_NAME} <{GIT_EMAIL}>"],
        cwd=worktree_path,
    ).returncode
    if rc != 0:
        # Nothing to commit (maybe content identical after copy?)
        _cleanup_worktree(infra_repo, branch, worktree_path)
        return

    # 7. Push
    if _run(["git", "push", "origin", branch], cwd=worktree_path).returncode != 0:
        _cleanup_worktree(infra_repo, branch, worktree_path)
        return

    # 8. Create PR
    title = f"hermes: sync skill update — {skill_list}"
    body = (
        "## 変更内容\n"
        "自動同期: skill_manage / curator によるスキル変更をinfra repoに反映\n\n"
        "### 変更されたスキル\n" + "\n".join(summary_lines) + "\n"
    )
    _run(
        [
            "gh", "pr", "create",
            "--repo", "turtton/infra",
            "--title", title,
            "--body", body,
            "--base", "main",
            "--head", branch,
        ],
        cwd=infra_repo,
        check=False,  # PR creation failure is logged but doesn't block cleanup
    )

    # 9. Cleanup worktree
    _cleanup_worktree(infra_repo, branch, worktree_path)

    logger.info("skill-gitops: PR created for branch %s", branch)


def _find_changed(
    skills: set[str],
    skills_dir: Path,
    infra_skills_dir: Path,
) -> set[str]:
    """Return subset of *skills* whose content actually differs from infra."""
    changed: set[str] = set()
    for name in skills:
        local = skills_dir / name / "SKILL.md"
        infra = infra_skills_dir / name / "SKILL.md"

        if not local.exists():
            continue  # skill was deleted locally — don't auto-sync deletion

        if not infra.exists():
            # Skill exists locally but not in infra yet — skip (new skills
            # need manual ConfigMap wiring)
            continue

        if local.read_bytes() != infra.read_bytes():
            changed.add(name)

        # Also check supporting files (references/, templates/, scripts/)
        local_dir = skills_dir / name
        infra_dir = infra_skills_dir / name
        for sub in ("references", "templates", "scripts", "assets"):
            l_sub = local_dir / sub
            i_sub = infra_dir / sub
            if l_sub.exists() and not _dirs_equal(l_sub, i_sub):
                changed.add(name)
                break

    return changed


def _dirs_equal(a: Path, b: Path) -> bool:
    """Recursive content comparison of two directories."""
    if not b.exists():
        return False
    a_files = sorted(a.rglob("*"))
    b_files = sorted(b.rglob("*"))
    if len(a_files) != len(b_files):
        return False
    for af, bf in zip(a_files, b_files):
        rel = af.relative_to(a)
        if rel != bf.relative_to(b):
            return False
        if af.is_file() and bf.is_file():
            if af.read_bytes() != bf.read_bytes():
                return False
    return True


def _sync_skill_dir(src: Path, dst: Path) -> None:
    """Mirror *src* skill directory into *dst*, preserving structure.

    Uses shutil.copytree with dirs_exist_ok to overwrite individual files
    without removing files in *dst* that don't exist in *src* (e.g.
    infra-specific metadata files).
    """
    for entry in src.iterdir():
        if entry.name.startswith("."):
            continue  # skip hidden files (.usage.json etc.)
        s_dst = dst / entry.name
        if entry.is_dir():
            shutil.copytree(entry, s_dst, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, s_dst)


def _cleanup_worktree(repo: Path, branch: str, worktree_path: Path) -> None:
    """Remove worktree, delete branch, and scrub the directory."""
    _run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo,
        check=False,
    )
    _run(["git", "branch", "-D", branch], cwd=repo, check=False)
    try:
        shutil.rmtree(worktree_path, ignore_errors=True)
    except OSError:
        pass
    _run(["git", "worktree", "prune"], cwd=repo, check=False)


# ── Shell helper ────────────────────────────────────────────────────────────


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Run a subprocess and return the result."""
    env = os.environ.copy()
    # Ensure gh uses the right git identity
    if "GIT_AUTHOR_NAME" not in env:
        env["GIT_AUTHOR_NAME"] = GIT_NAME
    if "GIT_COMMITTER_NAME" not in env:
        env["GIT_COMMITTER_NAME"] = GIT_NAME
    if "GIT_AUTHOR_EMAIL" not in env:
        env["GIT_AUTHOR_EMAIL"] = GIT_EMAIL
    if "GIT_COMMITTER_EMAIL" not in env:
        env["GIT_COMMITTER_EMAIL"] = GIT_EMAIL

    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        msg = f"Timed out after {timeout}s: {' '.join(args)}"
        if check:
            raise RuntimeError(msg) from e
        logger.warning("skill-gitops: %s", msg)
        return subprocess.CompletedProcess(args, -1, "", msg)

    if check and r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip() or "unknown error"
        raise RuntimeError(
            f"Command failed (exit={r.returncode}): {' '.join(args)}\n{err}"
        )

    return r
