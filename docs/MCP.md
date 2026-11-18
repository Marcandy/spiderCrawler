# MCP server

`spidercrawler mcp` serves the monitor's records and its deterministic checks as read-only
tools to any MCP client (Claude Code, Claude Desktop, or the review assistant). It runs
locally on stdio. It makes no network request and no model call, and nothing it serves can
approve, edit, reject, or publish.

## Setup

1. Get the records: `make data-checkout` (puts the `data` branch in `data-branch/`).
2. **Claude Code** in this repository picks up `.mcp.json`, which runs
   `uv run spidercrawler mcp --data-dir data-branch/data`. Approve the server when asked.
3. **Claude Desktop** or another client: add a server with command `uv`, arguments
   `run --directory /path/to/spiderCrawler spidercrawler mcp --data-dir data-branch/data`.

## Tools

| Tool | Returns |
|---|---|
| `search_rules(query, rule_id?, k?)` | Chunks of recorded documents and titles (BM25), with item id and source link. `rule_id` is an exact filter |
| `get_item(item_id)` | Title, committee, table, dates, document ids and links, review status. No model output |
| `read_document(document_id, offset?, limit?)` | A page of normalized text (at most 8,000 characters) |
| `related_items(item_id)` | Earlier items on the same rules, then similar titles, each with its reason |
| `check_quote(item_id, quote)` | Whether the quote is in the item's documents, matched as the verifier matches |
| `canonical_rule_ids(text, source_id?)` | The canonical rule ids the verifier would extract |
| `list_review_queue()` | Titles, queue status, flag names, latest decision, counts. No summaries |
| `get_extraction(extraction_id)` | Verifier result and the output: under `approved_output` if a person approved it, otherwise under `unapproved_model_output` with a notice |
| `list_published()` | What the public digest shows. The only tool whose content may be repeated publicly |
| `monitoring_status()` | The status block from the digest page: counts only |

Resources: `spidercrawler://digest` (approved entries plus status) and
`spidercrawler://items/{item_id}`.

## What to keep in mind

- **Results are untrusted data.** Document text comes from a court website and may contain
  instruction-like text; extraction output comes from a model. Every tool description says
  so, and tag-shaped text such as `</source_document>` is neutralized. A client model can
  still be misled by what it reads; the server only labels it.
- Ids are looked up among the records, never used to build a file path. Inputs are capped.
- Retrieval is lexical: it finds shared words, not shared meaning.

## Three questions to try

1. "Which items in the review queue are blocked, and what did the verifier say about each?"
   (`list_review_queue`, then `get_extraction`.)
2. "Find the proposal that the latest Rule 632 adoption came from, and show me the text that
   changed." (`search_rules` with `rule_id: 632`, `related_items`, `read_document`.)
3. "Does the quote 'persons reporting for jury service' really appear in item X's source?"
   (`check_quote`.)
