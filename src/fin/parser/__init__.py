"""Parser capability slice: the independent parser job service.

The parser service owns parser job state, parser attempts, cancellation, and
durable parsed-result existence. It owns no corpus identity, no attachment
readiness, no indexing, and no publication: fin keeps those authorities and
decides whether a result is usable (ADR 0103).

This package is a separate bounded context. It imports no other ``fin.*``
package, and its durable state lives in its own PostgreSQL schema
``fin_parser`` rather than in the tables the host and corpus contexts own.

Public surface:

* ``fin.parser.envelope`` — the versioned ``ParsedDocument`` result envelope
  every adapter emits (SC4).
* ``fin.parser.api`` — the ``/v1/jobs`` HTTP surface.
* ``fin.parser.worker`` — the resource-limited worker that claims attempts.
"""
