# 0079. One extract method per corpus version

Date: 2026-09-17
Status: accepted

Each **资料集合** / corpus **version** is built with **exactly one** extract method: **standard** or **Fast 抽图** (ADR 0067 / 0068). Switching the operator switch rebuilds that version (re-extract + Leiden + community reports, ADR 0078). Query **A/B/C** do not pick a second graph. Dual graphs (standard plus Fast) on one version are rejected.

Considered Options: keep both graphs and choose at query time; keep a standard graph always and add Fast only as an extra C layer.
