# 0016. thread_id is a session; run is one invoke

Date: 2026-08-21
Status: accepted

ADR 0011 treated `thread_id` as one question. That matches a toy Q&A, not a project-level research agent: follow-ups must keep checkpointed messages. v1 splits the session/run grain — `thread_id` is the conversation session (auto-create UUID if omitted, return it to the caller); `run_id` is one `invoke` on that thread. done / refuse / `step_cap` end the run, not the thread; a later question is a new run on the same `thread_id`. At most one active run per thread. Eval gold cases still use isolated threads so harness runs do not share history. Do not equate thread with user_id, and do not wait for a chat UI before adopting this grain.
