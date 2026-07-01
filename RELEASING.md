# Release process

Releases use semantic versions stored in `pyproject.toml` and annotated Git tags.

## Automated flow

1. The scheduled release job checks the `main` branch.
2. If there are no commits after the latest `v*` tag, it exits without publishing.
3. Otherwise it runs `python scripts/release.py --push`.
4. The script increments the patch version, commits it, creates an annotated tag, and atomically pushes both.
5. `.github/workflows/release.yml` validates that the tag matches `pyproject.toml` and creates the GitHub Release.

## Manual flow

Preview repository state first:

```powershell
git status --short
git log --oneline --decorate -10
```

Create a local release commit and tag:

```powershell
python scripts/release.py
```

After inspection, push them:

```powershell
git push origin main
git push origin <tag>
```

To create and push in one operation:

```powershell
python scripts/release.py --push
```

The script refuses to release from a dirty worktree, a branch other than `main`, or when no commits exist after the latest release tag.
