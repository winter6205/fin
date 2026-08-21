# 0020. Extracted ingest artifacts are the query SSOT

Date: 2026-08-22
Status: accepted

Page text and page-passage chunks are produced once by ingest from the frozen PDFs (pinned pdfplumber) into `data/corpus/extracted/` and committed. `read_file`, the BM25 retrieve adapter, and the citation verifier read only that tree. BM25 is built in RAM at process start. The ask path does not open a PDF; missing artifacts fail fast. Do not extract at query time, and do not store page text in the checkpointer database.
