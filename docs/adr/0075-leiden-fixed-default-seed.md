# 0075. Leiden uses a fixed default seed so communities are stable

Date: 2026-09-17
Status: accepted

Hierarchical Leiden (ADR 0065) uses a **fixed default random seed** so the same entity-relation graph yields the same community partition and ids across runs. The index job may change the seed to try another cut. Unseeded random clustering is rejected as the default (C reports would churn without a graph change).

Considered Options: new random seed every index; freeze one seed with no config.
