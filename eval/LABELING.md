# Labeling guide

Written **before** any item is labeled (PLAN.md section 8). Labels follow this guide, not
intuition, so the keyword baseline is not graded against labels that restate its keywords.
The maintainer writes the labels. A model must not write gold labels for an eval that grades a
model.

Each item is a folder `eval/items/<split>/<id>/` with `input.txt` (the normalized source text
the pipeline produces; do not edit it) and `gold.json` (you fill in `labels`).

## Fields

### `is_substantive`
- `true` if what the rule requires, permits, or forbids changes for anyone.
- `false` for typo fixes, renumbering only, relettering only, cross-reference updates, or
  comment-only changes (changes only inside a Comment or explanatory report).
- Hard case: a renumbering that also changes one word of a requirement is `true`.

### `stage`
- `proposed`: a notice of proposed rulemaking or publication report seeking comment.
- `adopted`: a Supreme Court order adopting, amending, or rescinding rules.
- `other`: anything else (workgroup reports, corrections notices).

### `rule_ids` (canonical)
- Only rules being adopted, amended, rescinded, renumbered, or proposed. Not rules that are
  merely cited ("as provided in Rule 1003").
- Write canonical form: `Pa.R.Crim.P. 632`; other sets as `Pa.R.A.P.`, `Pa.R.Civ.P.`,
  `Pa.R.E.`, `Pa.R.J.C.P.`, `Pa.R.O.C.P.`, `Pa.R.J.A.`, `Pa.R.Civ.P.M.D.J.`.
- Ranges as written but expanded: `Pa.R.Crim.P. 520.1-520.19`.
- Order does not matter; scoring compares sets.

### `effective_date` (ISO `YYYY-MM-DD` or `null`)
- Only a stated calendar date the change takes effect. A comment deadline is **not** an
  effective date; proposals are almost always `null`.
- Relative dates ("30 days after publication") are `null`; note them in `notes`.

### `expungement_relevance`
- `yes`: the change concerns expungement, sealing, limited access, or destruction of criminal
  history records, or a procedure whose only purpose is one of those (e.g. Rules 490, 790, 791,
  or ARD expungement under Rule 320).
- `no`: the change clearly does not touch any of those.
- `unsure`: the text touches records or dispositions in a way that might affect expungement
  but does not say so (e.g. changes to how dismissals are recorded).
- Decide from the document, not from what you know about the law.

### `hard`
- `true` if you hesitated on any field above or would expect two careful labelers to disagree.
  Abstention quality is measured against this flag.

## Injection items

Items whose `gold.json` has `"synthetic": true` contain instruction-like text inserted by
`spidercrawler eval-inject`. Label the **original** document (the inserted text changes
nothing). The eval expects the labels unchanged and the `suspicious_input` flag raised.

## Splits

- About 20 `dev` items and 30 `test` items, each split with positives, negatives, expungement
  items, and injection items (3 dev, 5 test).
- Tune prompts and verifier on `dev` only. Once `test` is labeled it is frozen: no edits
  after seeing model outputs. Any change made after looking at test results is reported.
- The stakeholder independently labels at least 10 `test` items into `gold.second.json`;
  agreement is reported per field.
