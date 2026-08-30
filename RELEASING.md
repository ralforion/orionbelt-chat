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
   heading and is checked by CI against the locked environment:

   ```bash
   uv sync --frozen --no-dev --python 3.13
   version=$(grep -m1 '^version = ' pyproject.toml | sed -E 's/^version = "([^"]+)"/\1/')
   .venv/bin/python scripts/gen_third_party_licenses.py \
     --venv .venv --version "$version" --overrides licenses \
     --fail-on-missing-notice -o THIRD_PARTY_LICENSES.md
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
stored anywhere. One-time setup, matching what the workflow declares:

| Field | Value |
|---|---|
| Owner | `ralforion` |
| Repository | `orionbelt-chat` |
| Workflow | `pypi-publish.yml` |
| Environment | `pypi` |

Because the project does not exist on PyPI yet, register it as a **pending
publisher** at <https://pypi.org/manage/account/publishing/>; PyPI converts it
into a normal trusted publisher on the first successful upload. Create the
`pypi` environment under Settings → Environments if you want uploads gated on a
required reviewer.

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
