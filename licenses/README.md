# Curated license texts

`scripts/gen_third_party_licenses.py` reproduces the license and NOTICE files
that each dependency ships inside its installed distribution. A few projects
declare a license in their metadata but ship the text in **neither** their wheel
nor their sdist — for those, the only authoritative copy lives in the project's
own repository.

This directory holds those copies, fetched by hand and recorded below. The
generator picks a file up by matching its stem against the PEP 503 normalized
distribution name (`logfire-api` → `logfire-api.txt`) and marks the resulting
block as curated, so a reader can tell it apart from a text that arrived in the
wheel.

Apache-2.0 packages are deliberately **not** listed here: the license is
identical for every one of them, so the generator reproduces a single verbatim
copy in the output's Appendix A instead.

| File | Distribution | Declared | Source |
| --- | --- | --- | --- |
| `lazify.txt` | Lazify | BSD-3-Clause | <https://github.com/numberly/lazify/blob/master/LICENSE> |
| `logfire-api.txt` | logfire-api | MIT | <https://github.com/pydantic/logfire/blob/main/LICENSE> |

## Adding one

`--fail-on-missing-notice` (which CI and the Docker build both pass) fails when
a non-Apache dependency has no license text from any source. When that fires:

1. Confirm the wheel and sdist really carry nothing.
2. Copy the text verbatim from the project's repository at the version in
   `uv.lock` — do not reconstruct it from an SPDX template, since the copyright
   line is specific to the holder.
3. Save it as `licenses/<normalized-name>.txt` and add a row above.
