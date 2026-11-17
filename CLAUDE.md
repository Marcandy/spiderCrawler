# Working rules for coding agents

spiderCrawler monitors the Pennsylvania Supreme Court criminal procedure rules committee
page. An LLM summarizes each new item, deterministic code verifies the summary, and a person
approves it before anything is published. Read `PLAN.md` before changing design, and
`docs/AGENTIC.md` before touching the repair loop, retrieval, the MCP server, or the assistant.

## The rule that outranks the rest

Nothing an LLM or an agent produces reaches the public digest, the public issue, or a
publishable state without a person's decision. No code path may approve, edit, or reject on a
person's behalf.

## Commands

```sh
uv sync            # install
make lint          # ruff check + ruff format --check
make cov           # pytest with the 90% coverage floor (what CI runs)
make test          # pytest only
actionlint         # after any change under .github/workflows/
```

Check the exit code of each before committing. `pytest | tee` hides it; use
`make cov > log 2>&1; echo $?`. Tests never touch the network or the API: fake clients only.

## Working agreements

- **One story per branch and PR.** Commit each step. Each PR body carries an adversarial
  review table: what could break, and how it was checked.
- **The owner merges.** Do not merge a PR or delete a branch unless told to, by name.
- **Every commit ends with both trailers:**
  ```
  Co-authored-by: Marcandy <16161555+Marcandy@users.noreply.github.com>
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```
  PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Gold labels are the owner's.** Never write, suggest, or pre-fill a label value, including
  retrieval link labels. Synthetic test items must look synthetic.
- **No network or API call without approval.** That includes `spidercrawler run`,
  `eval-collect`, `make eval-live`, and `assist` against the real API. State the expected
  cost when asking.
- **Ask first** before adding a dependency or changing the output schema, verifier rules,
  data model, or record format.
- **Records are append-only.** New kinds follow `records.py`: written once, never modified.
- **Every model call goes through `budget.py`.** The budget fails closed.
- **No secrets** (API key, contact email, request headers) in code, logs, traces, fixtures, or
  commit messages.
- **Before writing code that calls the Claude API,** load the `claude-api` skill. Do not
  write model ids, tool-use, or caching details from memory.
- **Docs do not oversell.** No feature is described as making outputs correct. Avoid
  "ensures", "guarantees", "prevents hallucination". Say what is measured and what is pending.
- **Style:** short module docstrings that say why, small typed functions, ruff clean, one
  concern per commit. Plain, short prose.

## Mistakes already made here

- A commit was pushed with a lint error because the exit code was not checked.
- `gh pr merge --delete-branch` on a stack base closed the dependent PR.
- Retargeting a PR does not trigger CI. Close and reopen it.
- `ruff format` can rewrite lines a later string-replace edit expects. Re-read before editing.
- A test asserting a string was absent passed because it checked the wrong string. Prefer
  positive assertions, or prove the absent-string test can fail.
