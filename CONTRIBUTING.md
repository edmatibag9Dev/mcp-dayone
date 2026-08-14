# Contributing Guidelines

This document defines the documentation and commit standards for all repositories maintained by Ed. These rules apply to every contributor — human or AI agent. No exceptions for small changes, prototypes, or "quick" pushes.

---

## Why These Standards Exist

A stub README or one-liner commit message is a broken deliverable. Other developers — and AI agents building on top of this work — rely on repository documentation to understand what was built, why it exists, and how to use or extend it. If the documentation is incomplete, the project is incomplete.

These standards were established after an AI agent pushed a bare-bones README with no feature descriptions, no usage instructions, and no context. That kind of push wastes everyone's time and creates technical debt immediately.

---

## Commit Message Format

Every commit must have a subject line and a body. One-liners are not acceptable for `feat`, `fix`, or `docs` commits.

### Format

```
<type>(<scope>): <short summary of what changed>

What was built or changed:
- <specific description of the change>
- <another change if applicable>

Key features or behavior introduced:
- <what this commit makes possible>
- <any important behavioral detail>

Files created or modified:
- <filename> — <what it does>
- <filename> — <what it does>

Known limitations introduced (if any):
- <anything hardcoded, incomplete, or fragile>
```

### Commit Types

| Type | When to use |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix or correction |
| `docs` | Documentation only (README, comments, guides) |
| `refactor` | Code restructure with no behavior change |
| `chore` | Dependency updates, config changes, cleanup |
| `data` | Data source changes, schema updates |

### Rules

- Subject line: 50 characters max, imperative mood ("Add login flow" not "Added login flow")
- Body: required for all `feat` and `fix` commits — minimum 3 bullet points
- Never use: "add file", "update", "fix stuff", "changes", "misc", "wip" as the full message
- If a commit touches a README, that is not a `docs` commit — use the type matching the underlying work

### Good Example

```
feat(auth): Add OAuth2 login flow with Google provider

What was built:
- Implemented Google OAuth2 authentication using the oauth2-proxy library
- Added session token storage with 24-hour expiry
- Created /auth/callback route to handle provider redirect

Key features:
- Users can sign in with Google without creating a separate password
- Session persists across browser restarts via secure cookie
- Failed auth attempts log to stderr with provider error code

Files created or modified:
- auth/oauth.py — OAuth2 client initialization and callback handler
- auth/session.py — Session token creation and validation
- config/oauth_config.yaml — Provider credentials (keys excluded, see .env.example)
- README.md — Updated setup instructions and environment variable list
```

### Bad Example (never do this)

```
update auth
```

---

## README Requirements

A `README.md` must be created or fully updated on every commit that adds files, introduces a feature, changes behavior, or modifies data sources. This is not optional.

### When to Update the README

- Any `feat` commit → README must be updated
- Any `fix` commit that changes observable behavior → README must be updated
- Any `data` commit → README data sources section must be updated
- `refactor` and `chore` commits → README update optional unless file names changed
- `docs` commits → README is the primary deliverable

### Required Sections

Every README must contain all of the following sections, in this order. If a section genuinely does not apply, write the heading and explain why ("No external data sources are used — all data is generated at runtime").

---

#### 1. Project Overview

2–4 sentences. What does this project do? What is it, technically? What does it produce or enable?

#### 2. Purpose

Why was this built? What problem does it solve? Who is it for? What would be harder or impossible without it?

#### 3. Features

Each major capability described in plain language. Not a bullet dump — write enough that someone unfamiliar with the code can understand what each feature does and when they'd use it.

#### 4. File Descriptions

Every file in the repository listed with its role. For directories, describe what lives inside. Example:

```
auth/oauth.py        — OAuth2 client and Google provider callback handler
auth/session.py      — Session token creation, validation, and expiry logic
config/              — YAML configuration files (credentials excluded)
README.md            — This file
```

#### 5. How to Use

Step-by-step instructions for each feature. Assume the reader has never seen this codebase. Include:
- Prerequisites and dependencies
- Installation or setup steps
- How to run or invoke each feature
- Expected output or behavior
- Any required environment variables or config values (use `.env.example` format, never real keys)

#### 6. Data Sources

For every external data source used: name, URL, what data is pulled, access requirements (API key, auth, rate limits). Example:

```
- Alpha Vantage (https://www.alphavantage.co/) — End-of-day OHLCV stock data. 
  Requires free API key. Rate limit: 5 requests/minute on free tier.
```

If no external data sources: write "No external data sources. All data is [generated/user-provided/etc.]."

#### 7. Known Limitations

Be honest. What doesn't work yet? What is hardcoded that should be configurable? What breaks under certain conditions? What hasn't been tested? If the project is fully functional and tested, write "No known limitations at time of this release" — but only if that's actually true.

#### 8. Workarounds

For each limitation listed above, describe the current workaround if one exists. If there is no workaround, say so. Example:

```
Limitation: GitHub API rate limit (60 req/hr unauthenticated) causes failures on large repos.
Workaround: Set GITHUB_TOKEN in .env to raise limit to 5,000 req/hr.
```

#### 9. Build Notes

Environment, dependencies, and non-obvious setup. Include:
- Python/Node/runtime version required
- How to install dependencies
- Any platform-specific behavior (e.g., "tested on macOS 14, not validated on Windows")
- Any sandbox or network restrictions relevant to deployment
- If the GitHub Contents API is used instead of `git push`, explain why and how

---

### README Minimum Length

400 words. A README shorter than this is almost certainly missing required content. Quality matters more than word count, but length is a useful self-check.

---

## Pushing Files via GitHub Contents API

When `git clone` is blocked by sandbox network restrictions (common in Claude Code / Cowork environments), use the GitHub Contents API to push files directly.

### Creating a new file

```
PUT /repos/{owner}/{repo}/contents/{path}

{
  "message": "<your full commit message here>",
  "content": "<base64-encoded file content>",
  "branch": "main"
}
```

### Updating an existing file

You **must** include the current file's `sha`. Missing it returns a 409 conflict.

1. GET `/repos/{owner}/{repo}/contents/{path}` — retrieve `content` (base64) and `sha`
2. Decode the base64 content and read it fully
3. Merge new content with existing — do not overwrite blindly
4. Base64-encode the final merged content
5. PUT with `message`, `content`, `sha`, and `branch`

```
PUT /repos/{owner}/{repo}/contents/{path}

{
  "message": "<your full commit message here>",
  "content": "<base64-encoded merged content>",
  "sha": "<sha from the GET response>",
  "branch": "main"
}
```

### Important

- Always base64-encode file content before the PUT — the API will reject plain text
- Commit message in the API call must follow the full commit format defined above
- Do not use this API to push secrets, credentials, or `.env` files with real values

---

## Self-Check Before Pushing

Before any push, confirm:

- [ ] Commit message has a subject line and a body with ≥ 3 bullets (for feat/fix)
- [ ] README.md exists in the repo root
- [ ] README has all 9 required sections
- [ ] README is ≥ 400 words
- [ ] All new files are listed in the File Descriptions section
- [ ] Data sources section is accurate and includes URLs
- [ ] If updating an existing README, current SHA was retrieved before PUT

---

## What "Bare-Bones" Means (and Why It's Not Acceptable)

A bare-bones push is any of the following:
- A README with only a project name and one sentence
- A commit message with no body
- A repo with files but no README at all
- A README that describes what the code *is* but not how to *use* it
- Sections present but left as placeholders ("TODO: add usage instructions")

These are incomplete deliverables. Push them only if you are mid-work and the branch is not meant for review. On any branch that represents finished or reviewable work, bare-bones is not acceptable.
