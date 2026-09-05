# 0045. Company corpus is the retrieve backend; dream is not ingest

Date: 2026-09-04
Status: accepted

Institutional materials (filings now; later connectors) enter the product by ingest and **push**, and are queried through the **retrieve port**. The typical retrieve backend is **iknow-memory**. Chat **extract** / **dream** is not how company documents get into that store. A switchable local buffer may still hold explicit operator pins; it is not the company corpus.

Considered Options: using dream to transport dialogue into the retrieve backend as if it were the corpus (rejected — mix cite-tree with chat); keeping company documents only on the fin host with no retrieve backend (rejected — ranking and durable search already belong behind the retrieve port).
