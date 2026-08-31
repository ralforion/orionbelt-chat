# Releasing

Releases are cut from `main` and driven entirely by pushing a version tag.

## Steps

1. **Bump the version in all four places** (they must agree, or the release
   workflow fails):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `orionbelt_chat/public/header.js` → `var VERSION = "vX.Y.Z";`
   - `README.md` → the version badge (`badge/version-X.Y.Z-brightgreen`)
   - `uv.lock` → run `uv lock` after bumping `pyproject.toml`; it pins the
     project's own version, and CI's `uv sync --frozen` fails if it's stale.

   Then regenerate the committed attribution, which carries the version in its
   heading and is checked by CI against the locked environment.

   **Regenerate it on Linux.** The resolved set is platform-dependent —
   `jeepney` and `SecretStorage` install there and not on macOS — so a file
   generated on a Mac is two packages short and CI rejects it. The image and
   the release assets are Linux, so Linux is the answer that is correct
   anyway. If you are not on Linux, run it in a container:

   ```bash
   docker run --rm --platform linux/amd64 -v "$PWD":/w -w /w python:3.13-slim bash -c '
     pip install -q uv
     UV_PROJECT_ENVIRONMENT=/tmp/venv uv sync --frozen --no-dev
     version=$(grep -m1 "^version = " pyproject.toml | sed -E "s/^version = \"([^\"]+)\"/\\1/")
     /tmp/venv/bin/python scripts/gen_third_party_licenses.py \
       --venv /tmp/venv --version "$version" --overrides licenses \
       --fail-on-missing-notice -o THIRD_PARTY_LICENSES.md
   '
   ```

2. **Open a PR, get CI green, merge to `main`.** Never tag off a branch.

3. **Tag the merge commit and push:**

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z — short summary"
   git push origin vX.Y.Z
   ```

That's the whole manual part. Pushing the tag triggers three workflows:

- **`docker-publish.yml`** builds and pushes the image to Docker Hub
  (`:X.Y.Z`, `:X.Y`, `:X`, `:latest`) and syncs the Docker Hub description.
- **`release.yml`** verifies the four version locations agree with the tag,
  then creates the GitHub Release with auto-generated notes and marks it
  Latest.
- **`pypi-publish.yml`** builds the sdist and wheel, checks the wheel really
  contains the packaged assets, and uploads to PyPI.

`release.yml` and `pypi-publish.yml` are each split into a `build` job and a
`publish` job, and the split is the security boundary rather than a staging
convenience. Everything that runs repository code or third-party tooling
happens in `build`, under a token that can neither write to the repository nor
mint an OIDC token. Each `publish` job checks out nothing and installs nothing:
it downloads the artifact `build` produced and hands it to one pinned action.
So the credential that can create a Release, and the identity that can upload
to PyPI, are never in scope while a build backend, a lockfile-resolved
dependency or an SBOM generator is running.

PyPI matches its trusted publisher on the workflow *filename* and the
environment, not the job, so `environment: pypi` lives on the `publish` job
that performs the OIDC exchange and the table below is unaffected by the split.

## PyPI

`pypi-publish.yml` uses PyPI **Trusted Publishing** (OIDC), so no API token is
stored anywhere. The publisher is configured as follows, matching what the
workflow declares:

| Field | Value |
|---|---|
| Owner | `ralforion` |
| Repository | `orionbelt-chat` |
| Workflow | `pypi-publish.yml` |
| Environment | `pypi` |

The trusted publisher is already live — the project has published to PyPI
since 1.0.0 in April 2026 — so there is nothing to register. The table above
documents the configuration that exists; it only needs revisiting if the
workflow filename, the environment or the repository name changes, because
those are what PyPI matches on.

The `pypi` environment exists under Settings → Environments. It carries no
required reviewer, so uploads are not gated on a manual approval; add one
there if you ever want them to be.

**A version can only be uploaded once** — PyPI rejects a re-upload of a filename
it has already seen, even after you delete the release — so a botched publish
needs a new patch version, not a retry.

## Notes

- **A git tag is not a GitHub Release.** Before `release.yml` existed, tagging
  built the image but left the Releases page untouched — that's the gap this
  workflow closes. Tag pushes now create the Release automatically.
- `release.yml` is idempotent: if the Release already exists it exits cleanly,
  so re-running a tag's workflow is safe.
- To re-cut a mistaken tag, delete it locally and on the remote
  (`git push origin :vX.Y.Z`), delete the GitHub Release if one was created,
  then repeat from step 3.
