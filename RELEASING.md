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
     Use a current uv — CI installs the latest — since an older one rewrites
     the lockfile's format along with the version.

   Then **move `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`**
   and leave a fresh, empty `## [Unreleased]` above it. That section *is* the
   release notes: `release.yml` publishes it as the GitHub Release body, so
   write it for readers, in the style of the other OrionBelt repositories —
   `### Added` / `Changed` / `Fixed` / `Dependencies` / `Internal`, each entry
   opening with a bold sentence and ending with its PR numbers, and an
   `### Upgrading from X.Y` section first when a default changed. CI fails a
   version bump that has no section (`tests/test_release_notes.py`). Add the
   version's link at the foot of the file and repoint `[Unreleased]`.

   Then regenerate the committed attribution. CI checks it against the locked
   environment, so it normally comes out unchanged; regenerating confirms it.

   **Regenerate it on Linux, resolved for 3.11.** Two things about the
   environment matter, and neither of them is the machine you happen to be on:

   - *Linux.* The resolved set is platform-dependent — `jeepney` and
     `SecretStorage` install there and not on macOS — so a file generated on
     a Mac is two packages short and CI rejects it. The image and the release
     assets are Linux, so Linux is the answer that is correct anyway.
   - *3.11, not the 3.13 the image ships.* The notice credits the union of
     every interpreter `requires-python` supports, and only the floor installs
     all of it: `backports-tarfile`, `importlib-metadata` and `zipp` carry
     `python_full_version < '3.12'` markers, so on 3.13 they are absent and
     the generator stops on three packages it cannot read. Over-crediting is
     the safe direction — the image contains a subset of what is credited,
     never more. See the comment on the `licences` job in `ci.yml`.

   If you are not on Linux, run it in a container:

   ```bash
   docker run --rm --platform linux/amd64 -v "$PWD":/w -w /w python:3.11-slim bash -c '
     pip install -q uv
     UV_PROJECT_ENVIRONMENT=/tmp/venv uv sync --frozen --no-dev --python 3.11
     uv pip install --python /tmp/venv/bin/python -q packaging
     /tmp/venv/bin/python scripts/third_party_notices.py
   '
   ```

   `packaging` goes in with `uv pip`, not `python -m pip`: the venv uv builds
   has no pip in it, and it cannot come from the dev extra either, since this
   environment is deliberately `--no-dev`.

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
- **`release.yml`** verifies that `pyproject.toml`, `header.js` and the README
  badge agree with the tag, then creates the GitHub Release with the version's
  `CHANGELOG.md` section as its notes (`scripts/release_notes.py`) and marks it
  Latest. `uv.lock` is the fourth place, but it is already
  guarded by CI's `uv sync --frozen` and is not re-checked here.
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
- `release.yml` is idempotent: if the Release already exists it refreshes the
  assets and leaves the notes alone, so re-running a tag's workflow is safe and
  does not undo an edit made on the Releases page. To republish the notes from
  `CHANGELOG.md` after fixing them there:
  `gh release edit vX.Y.Z --notes-file <(python3 scripts/release_notes.py X.Y.Z)`.
- To re-cut a mistaken tag, delete it locally and on the remote
  (`git push origin :vX.Y.Z`), delete the GitHub Release if one was created,
  then repeat from step 3.
