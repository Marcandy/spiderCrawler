# Limitations

Read this before relying on anything this tool produces.

## Scope

- Covers only the Pennsylvania Supreme Court **rules committee pages** on pacourts.us. It is not
  the official Pennsylvania Code or Pennsylvania Bulletin; those sites disallow automated
  access and are never fetched.
- Focused on the **Rules of Criminal Procedure**. The other seven committees are watched only
  for items that cite criminal rules.
- The committee pages list only recent adoptions. History before this tool's first run comes
  from a one-time manual collection for the eval set.
- **Not legal advice.** Summaries describe what changed. They never say who is eligible for
  anything, and the verifier's advice filter is a narrow pattern match, not a guarantee.

## What the safeguards do and do not prove

- The verifier checks that every quote **exists** in the source and that dates and rule ids
  agree with the committee page. It does **not** check that a claim is correct. A real quote
  can support a wrong summary ("supported but wrong"); only the human reviewer catches that.
- The main protection against prompt injection is the architecture: the model has no tools,
  can only return a fixed schema, a deterministic verifier runs, and a person approves every
  item. The injection tests confirm this holds for 8 synthetic cases. They are not proof of
  robustness against a determined attacker. Injected text in the test set is inserted as its
  own paragraph, which is easier to spot than text blended into a sentence.
- The `suspicious_input` flag is a regex. It is expected to miss paraphrased instructions;
  the eval measures how often.
- Quote matching ignores letter case and hyphens inside words, so "Court" and "court" match.

## Data and parsing

- PDF text extraction can miss underlined additions in some documents. Those documents are
  flagged `markup_lost` for the reviewer. Scanned PDFs without text are flagged `no_text` and
  are not sent to the model.
- Underlined text that is not an addition (a link, a heading) would be marked as added.
- The tool depends on the committee page template. A layout change, a flood of "new" items
  (for example if the site moves its PDFs), or a sharp drop in rows pauses that source until a
  person accepts the page with `spidercrawler run --seed --source <id>`.

## Evaluation

- The eval set is small (about 50 items), so every number has a wide interval.
- Labels are written mostly by the maintainer; a second labeler checks a subset.
- One model vendor. Model responses are not deterministic; the eval is reproducible only from
  its response cache.

## People

- At the time of writing there is **no external reviewer**. Every approval so far is the
  maintainer's own.
