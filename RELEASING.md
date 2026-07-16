# Releasing

Releases are cut from `main` and driven entirely by pushing a version tag.

## Steps

1. **Bump the version in all four places** (they must agree, or the release
   workflow fails):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `public/header.js` → `var VERSION = "vX.Y.Z";`
   - `README.md` → the version badge (`badge/version-X.Y.Z-brightgreen`)
   - `uv.lock` → run `uv lock` after bumping `pyproject.toml`; it pins the
     project's own version, and CI's `uv sync --frozen` fails if it's stale.

2. **Open a PR, get CI green, merge to `main`.** Never tag off a branch.

3. **Tag the merge commit and push:**

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z — short summary"
   git push origin vX.Y.Z
   ```

That's the whole manual part. Pushing the tag triggers two workflows:

- **`docker-publish.yml`** builds and pushes the image to Docker Hub
  (`:X.Y.Z`, `:X.Y`, `:X`, `:latest`) and syncs the Docker Hub description.
- **`release.yml`** verifies the four version locations agree with the tag,
  then creates the GitHub Release with auto-generated notes and marks it
  Latest.

## Notes

- **A git tag is not a GitHub Release.** Before `release.yml` existed, tagging
  built the image but left the Releases page untouched — that's the gap this
  workflow closes. Tag pushes now create the Release automatically.
- `release.yml` is idempotent: if the Release already exists it exits cleanly,
  so re-running a tag's workflow is safe.
- To re-cut a mistaken tag, delete it locally and on the remote
  (`git push origin :vX.Y.Z`), delete the GitHub Release if one was created,
  then repeat from step 3.
