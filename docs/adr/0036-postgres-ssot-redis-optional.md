# 0036. Postgres is authority SSOT; Redis is optional non-SSOT runtime

Date: 2026-08-29
Status: accepted

Postgres remains the only authority store for checkpoints, thread/run metadata, local memory buffer rows, and thumbs feedback. Redis may optionally provide rate-limit, fair queue, and Pub/Sub fan-out when `FIN_REDIS_URL` (name implementer-owned) is set; when unset, `fin-serve` stays single-process and must not require Redis. Redis must not store memory bodies, citation payloads, or checkpoint bytes as SSOT. Product paths that use Postgres should use connection pooling rather than one-connection-per-request.

Considered Options: require Redis at boot (hurts local/demo); put memory or turns in Redis as authority (split-brain with Postgres).
