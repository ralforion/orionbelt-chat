# Changelog

All notable changes to OrionBelt Chat will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before 1.7.0 are described on the
[GitHub Releases page](https://github.com/ralforion/orionbelt-chat/releases).

## [Unreleased]

### Added
- **Sign-in with OpenID Connect (SSO).** Set `OIDC_ISSUER`, `OIDC_CLIENT_ID`
  and a client secret, and users sign in with Keycloak, Entra ID, Okta or any
  other OIDC provider before the chat opens. `OIDC_ALLOWED_ROLES` limits it to
  users holding one of the named roles or groups. Keycloak realm roles are
  read from `realm_access.roles` without any mapper. Off by default: without
  `OIDC_ISSUER` nothing changes. See "Sign-in (SSO)" in the README.
- **The identity provider stays in charge of a sign-in.** Chainlit's session
  cookie lasts 15 days. The chat checks the sign-in on every request Chainlit
  authenticates (HTTP routes and websocket connections), at every new chat and
  at every message. It renews the access token as it expires and stops the
  session once the IdP refuses, or once the user no longer holds an allowed
  role. Roles are read from the tokens each renewal brings, never carried
  over from the sign-in's ID token. A user disabled, signed out or stripped
  of their role at the IdP no longer keeps access until the cookie runs out.
  New tokens that cannot be checked because the IdP's signing keys are out of
  reach are held unused until they can be, so a key outage does not end the
  sign-in. Signing out revokes the
  tokens at the IdP and ends the sign-in on the server, so a copy of the
  cookie kept from before is refused.
- `OIDC_CLIENT_AUTH`: `client_secret_post` (default) or `client_secret_basic`,
  for identity providers that register a client for HTTP Basic only (Okta's
  default).
- **Tokens stay on the server.** The browser's session cookie, signed but not
  encrypted, carries only a random handle to the sign-in, never a token.
  Chainlit's built-in Azure provider puts the refresh token in that cookie,
  and its Keycloak provider keeps it on an object every login shares; neither
  is used.

### Dependencies
- `pyjwt[crypto]` is now a direct dependency (it already came with Chainlit;
  the `crypto` extra checks RS256/ES256 signatures). No new packages ship.

## [1.7.1] - 2026-11-16

### Security
- **The Docker image no longer ships dependencies with known
  denial-of-service advisories.** `dnspython` 2.9.2 fixes excessive memory
  and CPU use (GHSA-vvf4-9x63-8xvv, high), and `opentelemetry-api` 1.45.2
  fixes quadratic-time `traceparent` parsing (GHSA-mf5v-w3w3-f6cg, medium).
  The 1.7.0 image locked 2.9.0 and 1.45.1, so it stays affected until it is
  replaced; a PyPI install that resolves its dependencies afresh picks up
  the fixed versions either way. (#105)

### Dependencies
- `pydantic-ai` 2.68.0 and `genai-prices` 0.2.3. (#106)

### Internal
- **Release notes come from `CHANGELOG.md`.** The release workflow publishes
  the tagged version's section here instead of GitHub's generated list of
  pull requests, which buried the features among the dependency bumps. A
  version bump without a section fails CI, so a tag cannot go out without
  its notes. (#104)
- Dev tooling: `pytest-asyncio` 1.5.0, `ruff` 0.17.5, `types-pyyaml`
  6.0.12.20261111. (#106, #107)

## [1.7.0] - 2026-11-12

### Upgrading from 1.6
- **MCP servers' instructions now reach the model.** The instructions a
  server sends when it connects were dropped before; they now go out with
  every request, each under its server's name and capped at 16,000
  characters. To keep a server's out, give its entry `instructions: false`
  in `mcp_servers.yaml`; for a built-in OrionBelt server, add an entry
  under its name. Without skills configured, nothing else changes: the
  greeting and the composer look as they did. (#102)

### Added
- **Agent Skills: `SKILL.md` folders the assistant loads when a task calls
  for one.** A skill is a folder holding a `SKILL.md` — `name` and
  `description` frontmatter, then markdown instructions — in the Agent
  Skills format, so skills written for other agents work here too. Folders
  in `./skills` and `~/.orionbelt-chat/skills` are picked up, or wherever
  the new `SKILLS_DIR` setting points; in Docker, mount one at
  `/app/runtime/skills`. Each request carries only the names and
  descriptions: the model loads a skill's instructions with `load_skill`,
  and a file they refer to with `read_skill_file`, only when the task needs
  it, and a loaded skill is never trimmed from the history. Nothing in a
  skill is executed, and reads stay inside its folder. A `SKILL.md` that
  cannot be used is named in the greeting with the reason. (#102)
- **Skills published by MCP servers are offered too.** A connected server
  serving `skill://<name>/SKILL.md` resources, as FastMCP's
  `SkillsDirectoryProvider` does, has its skills read over the same
  connection, so OrionBelt Analytics' `analytical-workflow`,
  `chart-examples`, `fan-trap-prevention` and `sql-best-practices` now
  reach the model. A local skill of the same name takes precedence, and
  `skills: false` on a server's entry leaves its skills out. (#102)
- **A Skills button under the message box switches skills on and off.** It
  opens a floating checklist of every skill, with its description and where
  it comes from, plus All and None. A change applies to the running chat
  at once, history kept, and is remembered in the browser, so each user of
  a shared deployment keeps their own. A loaded skill shows as a
  "Skill: name" step, and the answer it shaped ends with a "Skills used"
  line. (#102)
- **MCP elicitation: servers can ask you for input during a tool call.**
  Form mode renders the server's schema as an in-chat form with Submit,
  Decline and Cancel. URL mode shows the full link with its domain
  highlighted, warns about look-alike or unencrypted hosts, and opens it
  only when you click. Both protocol 2025-11-25 (`elicitation/create`) and
  2026-07-28 (`InputRequiredResult`) are handled. New settings:
  `MCP_ALLOW_ELICITATION` (default `true`) and
  `MCP_ELICITATION_TIMEOUT_SECONDS` (default `120`). (#80)

### Dependencies
- `pydantic-ai` 2.66.0, `mcp` 1.30.0, `fastmcp-slim` 3.4.10, `openai`
  3.35.1, `anthropic` 1.15.0, `pydantic-settings` 2.16.2, `python-dotenv`
  1.2.4, and security updates to `aiohttp` 3.14.4, `multidict` 6.9.1 and
  `urllib3` 2.8.0. (#71, #75, #79, #82, #83, #85, #86, #87, #88, #89, #93,
  #95, #99)

### Internal
- `main` merges through a merge queue, and CI runs the required checks on
  `merge_group` events. (#77)
- The third-party notice is built by the notice generator shared with the
  other OrionBelt repos. It names the licence BUSL-1.1, no longer calls
  MPL-2.0 permissive, and acknowledges `bidict` on the same footing as
  `certifi`, and the release check resolves it on Python 3.11, as CI does.
  (#67, #68, #69, #73)
- Dependabot groups security updates into one PR and no longer proposes
  Python minor or major bumps for the image. (#66, #94)
- `RELEASING.md` corrections. (#65, #72)
- Dev and CI tooling: `pytest-asyncio` 1.4.1.post1, `types-pyyaml`
  6.0.12.20261103, `astral-sh/setup-uv` 10.3.3, `docker/login-action`
  4.8.0, `docker/setup-qemu-action` 4.3.0, and other Actions updates. (#70,
  #74, #76, #81, #84, #90, #96, #97, #98, #100, #101)

[Unreleased]: https://github.com/ralforion/orionbelt-chat/compare/v1.7.1...HEAD
[1.7.1]: https://github.com/ralforion/orionbelt-chat/releases/tag/v1.7.1
[1.7.0]: https://github.com/ralforion/orionbelt-chat/releases/tag/v1.7.0
