# Agent Instructions — Repository Documentation Standards

This file governs how AI agents (Claude Code, Cowork, or any LLM-based tool) must behave when working in this repository. Read this file before making any changes. These instructions are non-negotiable and override any default agent behavior.

---

## Primary Rule

**Every push must include a detailed commit message and a fully updated README.md.**

This is not a style preference. It is a hard requirement. A push without a proper README update or with a one-liner commit message is a failed task, not a completed one.

---

## Before You Write Any Code

1. Check if a `README.md` exists in the repo root — `GET /repos/{owner}/{repo}/contents/README.md`
2. If it exists, read its full content and note its `sha` — you will need both
3. Read `CONTRIBUTING.md` in full — it defines commit format, README sections, and API push mechanics
4. Plan your README update alongside your code changes, not after

---

## Commit Message — Required Format

```
<type>(<scope>): <what changed>

What was built or changed:
- <specific item>
- <specific item>

Key features or behavior:
- <what this makes possible>
- <behavioral detail>

Files created or modified:
- <filename> — <role>

Known limitations (if any):
- <anything incomplete or hardcoded>
```

**Never** use one-liners. **Never** use: "add file", "update", "fix", "changes", "misc", "wip".  
`feat` and `fix` commits require a body with ≥ 3 bullet points.

---

## README — Required Every Time

Update `README.md` on every `feat`, `fix`, or `data` commit. The README must contain these 9 sections:

1. **Project Overview** — what it does (2–4 sentences)
2. **Purpose** — why it was built, what problem it solves
3. **Features** — each capability in plain language
4. **File Descriptions** — every file with its role
5. **How to Use** — step-by-step per feature, including prerequisites
6. **Data Sources** — name, URL, access requirements; or state "No external data sources"
7. **Known Limitations** — honest list of what doesn't work or is incomplete
8. **Workarounds** — how to work around each limitation
9. **Build Notes** — runtime version, dependencies, platform notes, any network restrictions

Minimum length: 400 words. Do not leave sections as placeholders.

---

## Pushing Files — GitHub Contents API

Use this when `git clone` is blocked by sandbox network restrictions.

### New file
```
PUT /repos/{owner}/{repo}/contents/{path}
{
  "message": "<full commit message>",
  "content": "<base64-encoded content>",
  "branch": "main"
}
```

### Existing file (README updates, edits)
```
# Step 1: Get current file
GET /repos/{owner}/{repo}/contents/{path}
→ save: content (decode from base64), sha

# Step 2: Merge your changes into the decoded content

# Step 3: Push
PUT /repos/{owner}/{repo}/contents/{path}
{
  "message": "<full commit message>",
  "content": "<base64-encoded merged content>",
  "sha": "<sha from GET>",
  "branch": "main"
}
```

**Critical:** Missing the `sha` on an update causes a 409 conflict. Always GET before PUT on existing files.  
**Critical:** Always base64-encode content before sending — plain text will be rejected.

---

## Self-Check Before Every Push

Run through this list mentally before calling the API:

- [ ] Commit message has subject + body with ≥ 3 bullets (feat/fix)
- [ ] README.md exists and has all 9 required sections
- [ ] README is ≥ 400 words
- [ ] All new files appear in the File Descriptions section
- [ ] Data Sources section is accurate with URLs
- [ ] If updating README, SHA was retrieved via GET first

---

## What Triggers a README Update

| Change type | README update required? |
|---|---|
| New feature added | Yes — always |
| Bug fix that changes behavior | Yes |
| New file added to repo | Yes — add to File Descriptions |
| New data source added | Yes — add to Data Sources |
| Refactor (no behavior change) | Only if filenames changed |
| Config or dependency update | Only if setup steps changed |
| README-only edit | Yes — that IS the commit |

---

## What a Completed Task Looks Like

A task is only complete when:
1. The code or data change works as intended
2. The commit message follows the full format above
3. The README is updated with all relevant changes reflected
4. The push to GitHub succeeded (verify with a GET on the file after pushing)

A working script with a stub README is an incomplete task. A detailed README with broken code is also an incomplete task. Both are required.

---

## Source of Truth

These instructions originate from `CONTRIBUTING.md` in this repository. If there is ever a conflict between `AGENTS.md` and `CONTRIBUTING.md`, `CONTRIBUTING.md` takes precedence. Both files should be kept in sync.
