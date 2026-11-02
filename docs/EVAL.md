# Evaluation

**Status (2026-11-01): no results yet.** The harness, metrics, and labeling guide are built
and tested; the labeled set and the first scored run are not done. This page will report
numbers only after the test split has been labeled by a person and scored once. Until then,
nothing in this repository claims any accuracy.

## What is measured

The extractor (one LLM call per rulemaking item) and the deterministic verifier, on a
hand-labeled set of historical items from the Pennsylvania Supreme Court rules committee pages.

| Metric | Question it answers |
|---|---|
| Field accuracy | Are rule ids (as a set), effective date, stage, expungement relevance, and "substantive" right? |
| Unsupported-claim rate | How often does the model quote text that is not in the source, before the verifier? |
| Block precision | When the verifier blocks an item, was the item really wrong? |
| False-block rate | How often does the verifier block an item that was fully right? |
| Supported-but-wrong rate | How often is every quote real but some field still wrong? **The verifier cannot catch this; only the human reviewer can.** |
| Abstention | Of "unsure" answers, how many were on items labeled hard, and of hard items, how many got "unsure"? |
| Injection: output unaffected | On synthetic items with inserted instructions, is the answer the same as on the original item? |
| Injection: flag rate | Did `suspicious_input` fire? Reported separately for the 5 phrasings the flag regex knows and the 3 paraphrased ones it is not designed to see. |
| Keyword baseline | Does a regex (expungement words; changed text plus a rule number) do as well on relevance and substantive? If it does, this page will say so. |

Every number is reported as a count with a 95% Wilson interval. The set is small (about 50
items), so intervals will be wide.

## The set

- About 20 **dev** and 30 **test** items: proposals and adoptions from the criminal committee
  (including the few expungement items: Rules 490, 790, 791), negatives from other committees
  and non-substantive edits, and 8 synthetic injection items (3 dev, 5 test), all marked
  synthetic.
- Labels follow [`eval/LABELING.md`](../eval/LABELING.md), written before any labeling.
  Labels are written by the maintainer, not by a model.
- Prompts and the verifier are tuned on dev only. The test split is frozen once labeled.
  Every scoring of the test split is counted and printed in the report.
- A second person labels at least 10 test items; agreement is reported per field.

## Reproducing

```sh
make eval        # dev split, offline from eval/cache (fails if a response is missing)
make eval-live   # dev split, calls the API for missing responses (costs money)
make eval-test   # the frozen test split
```

Responses are cached by prompt version, model, effort, schema version, and input, so the
report can be regenerated offline. Each report records model id, effort, prompt version,
date, and the token cost of the cached responses.

## Results

_Pending: labeled set and first scored run._
