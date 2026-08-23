# 0024. Conversation slice is a workspace web UI server; no interactive CLI

Date: 2026-08-22
Status: accepted

ADR 0023 required one conversation increment instead of another one-shot CLI or `/ask` milestone. That increment ships as a **workspace**: one web UI server that lists, opens, and resumes research threads, streams the in-flight run, and shows citations on the assistant turn. Streaming HTTP is that UI's transport on the same compiled graph, not a second milestone. Interactive CLI REPL is out of this increment — ordinary users will not talk in a terminal; v1 one-shot CLI and `POST /ask` stay as eval/script adapters. Visual layout is not this decision.
