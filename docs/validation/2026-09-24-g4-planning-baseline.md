# G4 planning baseline — fin (2026-09-24)

This records existing gold behavior at fin base `4f4ad7e1` before G4 implementation. It is not G4 attachment acceptance: the new attachment/index/read contract and its real-service gold cases do not exist yet.

## Existing nine-case answer gold

Executed from the G4 worktree with the sibling fin virtual environment:

```text
PYTHONPATH=src /home/winner/projects/fin/.venv/bin/pytest -q -m eval tests/eval/test_gold.py -vv
exit 0; 9 passed; terminal counts: done=6, refuse=3, step_cap=0, unknown=0.
```

| Case | Observed test result | Terminal class |
| --- | --- | --- |
| `fact_01_aapl_2022q3_total_net_sales` | PASS | done |
| `fact_02_msft_2023q1_cash_and_short_term_investments` | PASS | done |
| `fact_03_amzn_2023q1_total_net_sales` | PASS | done |
| `xperiod_01_aapl_total_net_sales_q1_to_q3_2023` | PASS | done |
| `xperiod_02_msft_net_income_q1_q2_q3_2023` | PASS | done |
| `xperiod_03_amzn_total_net_sales_q1_to_q3_2023` | PASS | done |
| `refuse_01_unknown_locator_no_repair` | PASS | refuse |
| `refuse_02_unciteable_answer_hits_repair_cap` | PASS | refuse |
| `refuse_03_cites_page_not_read_this_run` | PASS | refuse |

These cases use the existing extracted-tree read port and scripted model, so they do not prove a live iknow-memory store or G4 attachment behavior.

## Existing real MCP process baseline

```text
PYTHONPATH=src /home/winner/projects/fin/.venv/bin/pytest -q -m mcp tests/retrieve/test_live_research_cite_on_ingest.py -vv
exit 0; 2 passed.
```

| Case | Observed result |
| --- | --- |
| `research_done_cites_this_runs_retrieved_text` | PASS |
| `ungrounded_answer_over_the_same_live_evidence_is_not_done` | PASS |

This invokes a real iknow-memory MCP process, but that test uses in-memory iknow storage and a scripted model. The G4 real gold run specified in `specs/thread-attachment-source-read.md` must additionally exercise durable fin thread/object storage, the live iknow-memory store, attachment formats, continuation, and citation outcomes.

## G4 validation state

Not run: G4 implementation and its committed gold cases do not exist yet. The plan requires a new real-service validation record with per-case actual source identities, read bounds, statuses, and citations before G4 can be called complete.
