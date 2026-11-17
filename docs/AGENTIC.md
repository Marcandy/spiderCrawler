# The agentic parts, and what they do not prove

Four additions around the original single LLM call. Each section says what it does, the design
choice and the alternative rejected, what is measured, and what it does not prove.

Two facts limit every claim on this page (2026-11-16): no live run has succeeded, so none of
this has made a real model call; and no eval item is labeled, so every number is pending.

The tension the design answers: the original guards rely on the model having no tools and
only a fixed output schema. Each addition below keeps that true for the extraction call, and
keeps the human gate as the only path to a public surface.

## 1. Verifier-feedback repair loop and run trace

_Not built yet._

## 2. Retrieval over the rule corpus

_Not built yet._

## 3. Read-only MCP server

_Not built yet._

## 4. Review assistant (the one tool-using agent)

_Not built yet._
