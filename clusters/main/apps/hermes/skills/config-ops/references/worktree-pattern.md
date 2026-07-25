# Git Worktree Pattern: Bare Clone + Isolated Branch Worktrees

Use this when managing multiple repos from bare mirrors, or when you need an isolated working directory without disturbing the main checkout.

## Why bare clones + worktrees?

- **Single bare clone** acts as the git object store — no need to `git clone` fresh every time.
- **Worktrees** are cheap, isolated working directories — each gets its own branch, and you can have many in parallel.
- **Cleanup** is trivial: `git worktree remove <dir>` — no lingering branches in a stale checkout.

## Setup: Cloning as a bare mirror

```bash
# One-time setup — clone as bare (no working tree)
git clone --bare https://github.com/turtton/omp-flake.git ./omp-flake

# Or if you already have a regular clone, convert to bare:
cd existing-repo && git fetch --unshallow && git clone --mirror . ../repo-bare
```

The bare clone stores all objects and refs but has no index/working tree.

## Workflow: Worktree per branch

### 1. Create worktree from a new branch

```bash
# Inside the bare clone directory
git worktree add --checkout -b chore/my-branch /tmp/repo-workdir main
```

This creates branch `chore/my-branch` based on `main`, checked out into `/tmp/repo-workdir/`, leaving the bare clone clean.

### 2. Make changes in the worktree

```bash
cd /tmp/repo-workdir
# Edit files with patch/write_file, test, validate...
git add -A
git commit -m "chore: summary of changes"
```

The worktree is a **full git working directory** — all normal git commands work inside it.

### 3. Push and create PR

```bash
cd /tmp/repo-workdir
git push origin HEAD
gh pr create --base main --head $(git branch --show-current) --title "..." --body "..."
```

### 4. Clean up

```bash
cd /path/to/repo.bare
git worktree remove /tmp/repo-workdir
```

The worktree directory is deleted. The bare clone remains for the next branch.

## Managing multiple repos in batch

For multi-repo operations (e.g., standardizing 4 flake repos in parallel):

```bash
# Structure:
flakes-inspect/
├── omp-flake/           # bare clone
├── kotlin-lsp-flake/    # bare clone
├── intent-system-flake/ # bare clone
├── senpi-flake/         # bare clone
└── .hermes/plans/       # repo-wide plans

# Create worktree per repo:
for REPO in omp-flake kotlin-lsp-flake intent-system-flake senpi-flake; do
  git -C "flakes-inspect/$REPO" worktree add \
    --checkout -b "chore/standardize-ci" \
    "/tmp/$REPO-workdir" main
done
```

## Pitfalls

- **Worktree inside a worktree**: Don't run `git worktree add` from inside another worktree. Always run it from the bare clone.
- **Use absolute paths** for the worktree directory to avoid confusion when switching directories.
- **A branch can only be checked out in one worktree at a time.** Use unique branch names per worktree.
- **Remove before re-adding**: If you re-create the same worktree path, `git worktree remove` first — it won't overwrite.
