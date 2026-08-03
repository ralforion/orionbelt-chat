# EU AI Act — transparency posture

How OrionBelt Chat meets the Article 50 transparency obligations of Regulation
(EU) 2024/1689, and why the technical choices are what they are. Article 50
applies from **2 August 2026**.

This document records engineering reasoning, not legal advice.

## Who is responsible

The Act's free-and-open-source exemption (Art. 2(12)) does **not** cover
systems falling under Article 50 — transparency obligations survive the
exemption by explicit carve-out. OrionBelt Chat is in any case distributed
under BSL 1.1, which is source-available rather than free and open source, so
the exemption never applied. Art. 3(3) makes a provider a provider "whether
for payment or free of charge", so free distribution is not a shield either.

Anyone who clones this repository and runs it is a **deployer**, with their own
obligations. Under Art. 25 they additionally become a **provider** if they put
their own name or trademark on it, substantially modify it, or change its
intended purpose. Responsibility multiplies down the chain; it does not
transfer away from the upstream provider.

The practical consequence for this codebase: marking ships **on by default** so
downstream deployers inherit compliance rather than having to add it.

## Art. 50(1) — disclosure of AI interaction

A natural person interacting with an AI system must be informed of that fact at
the time of the first interaction. Disclosed in three places:

| Where | Source |
| --- | --- |
| UI chrome — "OrionBelt Chat – AI Assistant" | `.chainlit/config.toml`, `public/header.js` |
| Welcome screen notice | `chainlit.md` |
| First message of every session | `AI_DISCLOSURE` in `app.py` |

The first-interaction message is sent *before* any agent work, so it reaches
the user even when the agent or its MCP servers fail to start.

Pinned by `tests/test_ai_disclosure.py`.

## Art. 50(2) — marking of generated output

Output must be marked in a machine-readable format and detectable as
artificially generated, using solutions that are effective, interoperable,
robust and reliable **as far as technically feasible**, accounting for the
specificities and limitations of each content type.

All marking derives from one provenance record built per turn in
`src/provenance.py`, so every channel states the same thing. The record carries
the IPTC `digitalSourceType` term `trainedAlgorithmicMedia` — the interoperable
vocabulary shared with C2PA — plus the producing application, version, model
and timestamp.

| Channel | Marking | Source |
| --- | --- | --- |
| TTL, SPARQL, YAML downloads | `#` comment header | `mark_text` |
| SQL downloads | `--` comment header | `mark_text` |
| XML, RDF downloads | XML comment, after the declaration | `_mark_xml` |
| JSON downloads | `_provenance` object | `_mark_json` |
| JSON-LD downloads | `prov:wasGeneratedBy`, with the `prov` prefix added to `@context` | `_mark_json` |
| CSV, TSV downloads | adjacent `.prov.json` sidecar | `_marked_files` |
| PNG images from tools | XMP packet in an `iTXt` chunk | `mark_png` |
| Plotly charts | figure `meta` block plus a visible caption | `mark_figure` |
| Rendered page | `<meta name="ai-generated">` and `data-ai-generated` on non-user steps | `public/header.js` |

Pinned by `tests/test_ai_provenance.py`.

### Why these choices

**Marking never corrupts the payload.** Every helper is a no-op when it cannot
mark safely: unknown extensions pass through, unparseable JSON passes through,
a top-level JSON array is not wrapped (that would change how consumers parse
it), and non-PNG bytes are returned untouched. An XML comment is placed after
the declaration because a declaration is only well-formed as the very first
construct in a document.

**CSV and TSV get a sidecar, not a header.** A leading `#` line breaks strict
CSV parsers. Marking a data file by making it unreadable is not "effective".

**Charts are marked twice.** The `meta` block rides along in the figure JSON
the frontend receives, but Plotly's modebar PNG export is entirely client-side
and drops it. The visible caption is the only marking that survives that
export path.

**PNG XMP is written by hand.** A `iTXt` chunk with the `XML:com.adobe.xmp`
keyword is ~30 lines of `struct` and `zlib`, so image marking adds no runtime
dependency. Marking is idempotent — an already-marked PNG is returned as-is.

**DOM markers are a supplement, not the primary mechanism.** They make the
rendered page machine-inspectable, but they do not survive copy-paste. Content
that actually leaves the app is marked in-band.

### Why text watermarking is not attempted

Statistical watermarking of the chat prose itself (SynthID-Text and
equivalents) is **not technically feasible here**, and the Article 50(2)
feasibility qualifier is the basis for not attempting it:

- It requires logit-level access at generation time. This app is a client over
  OpenRouter, MLX, Ollama, Anthropic and OpenAI; it never sees logits, and no
  marking it applied would be consistent across providers.
- The zero-width-character alternative would silently corrupt the SQL, Turtle
  and CSV that users copy out of the chat — content whose correctness is the
  product.

The in-band marking above covers the payloads that leave the app, which is
where detection actually matters.

### C2PA Content Credentials — for deployers

C2PA signing is deliberately **not** enabled by default, and no signing key
ships in this repository. A private key in a public repository is worse than no
signing at all: every deployment would share one identity that anyone could
forge manifests against.

The XMP marking above is unsigned and needs no PKI — Art. 50(2) requires
content to be machine-readable and detectable, not cryptographically attested.
Deployers who want signed Content Credentials on chart images should supply
their own certificate and sign `mark_png` output before display. A self-signed
certificate produces a structurally valid manifest, but validators check the
signer against the C2PA trust list and will report it as an unknown source, so
it is suitable for verifying a pipeline, not for production provenance.

## Scope note

Much of what this app surfaces is query results and charts derived from the
deployer's own data via MCP tools, rather than synthetic media — arguably close
to the Art. 50(2) exemption for systems performing an assistive function that
does not substantially alter input data. The model-authored prose and the
generated SQL and Turtle clearly are generated content.

Marking is cheap enough that the code does not attempt to draw that line: every
channel is marked. The distinction is recorded here rather than encoded as
behaviour.
