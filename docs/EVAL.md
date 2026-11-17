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

With `--repair N` the report adds, for the repair loop ([AGENTIC.md](AGENTIC.md) section 1):

| Metric | Question it answers |
|---|---|
| Block rate, before and after | How many items the verifier blocks on the first attempt, and after up to N repair calls. |
| Supported-but-wrong, before and after | **The test that matters.** If repair lowers the block rate by raising this rate, it turned visible failures into invisible ones. |
| Attempts and tokens per item | What the loop costs. |

`spidercrawler eval --prompt v2` scores the prompt that adds retrieved context, on dev only,
and writes `eval/reports/dev-v2.md` beside the v1 report so the two can be compared: field
accuracy, supported-but-wrong, and tokens per item. The test split scores only the configured
pipeline; `--prompt` and `--repair` variants are refused there.

### Retrieval eval

Does `related_items` find an adoption's proposal? The owner labels links with
`spidercrawler eval-link` (it shows the adopted item and no candidate; `eval-link --list`
prints proposals in id order, not ranked). `spidercrawler eval-retrieval -k 5` then reports
recall@k and MRR with counts. Looking proposals up with `spidercrawler search` while
labeling would bias the labels toward what the retriever already finds; use the list.
**Status: pending, no link is labeled.**

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

## Building the labeled set

Five steps. Only step 2 touches the network. A person types every label; no command writes,
suggests, or pre-fills one.

1. **Sample.** `spidercrawler eval-sample --seed 7` writes `eval/sample-plan.json`: criminal
   proposals and adoptions, every Rule 490/790/791 item, negatives from other committees, and
   the injection slots (3 dev, 5 test). It fetches nothing. Review the plan first.
2. **Collect.** `spidercrawler eval-collect --plan eval/sample-plan.json --split dev`, then
   `--split test`. Each call makes at most 30 polite requests; run it again until every item
   is exported. Then `spidercrawler eval-inject --plan eval/sample-plan.json`.
3. **Label.** `spidercrawler eval-label` shows the next unlabeled item's source text and the
   guide's definition of each field, and asks for each value. It never shows model output.
   `spidercrawler eval-status` shows what is left.
4. **Freeze.** `spidercrawler eval-freeze` records a hash of every test item's text and
   labels. After that a test item can be edited only with `eval-label --unfreeze --reason`,
   which is logged in `eval/items/freeze-log.jsonl`, and `make eval-test` refuses to score
   until `eval-freeze --refreeze`.
5. **Second labeler.** `spidercrawler eval-label --second --split test` on at least 10 test
   items, then `spidercrawler eval-agreement --docs docs/EVAL.md` fills in the table below.

## Reproducing

```sh
make eval        # dev split, offline from eval/cache (fails if a response is missing)
make eval-live   # dev split, calls the API for missing responses (costs money)
make eval-test   # the frozen test split
uv run spidercrawler eval --split dev --repair 1   # with the repair loop (dev only)
```

Responses are cached by prompt version, model, effort, schema version, and input (a repair
attempt also by its number and a hash of the conversation it answered), so the
report can be regenerated offline. Each report records model id, effort, prompt version,
date, and the token cost of the cached responses.

## Results

_Pending: labeled set and first scored run._

### Label agreement

Agreement between the maintainer's labels and a second labeler's, per field: count, percent,
and Cohen's kappa for the fields with a fixed set of values. The test report repeats it.

<!-- agreement:start -->
_Pending: no item has second labels yet._
<!-- agreement:end -->
