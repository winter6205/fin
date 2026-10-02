-- Synthetic enterprise operations ledger — canonical shape and read-only boundary.
--
-- This is a DEVELOPMENT fixture. It is not a business source and no real
-- operator's data is in it. It exists so the `sql` 档's read path, its
-- database-enforced read-only boundary, and its failure taxonomy can be
-- exercised end to end while the real ledger stays blocked
-- (specs/search-mode-business-sql-contract.md §1.2, §2.4).
--
-- Two substitutions remain forbidden and neither is made here: fin's
-- checkpoint database and iknow's corpus/index Postgres are not the business
-- source, and the public market data loaded for infrastructure proof is not it
-- either. This file is a ledger, on its own instance, with its own role.
--
-- Expected values for acceptance are asserted by the tests against the values
-- written HERE, not read back from the database — a seed that silently loaded
-- nothing must fail the suite rather than pass it vacuously.

CREATE SCHEMA IF NOT EXISTS ledger;

-- Canonical contract shape: the reference, not a demand. A real ledger reaches
-- this through an authorized view or a field map (spec §2.1); no enterprise has
-- to reshape its database to match.
CREATE TABLE IF NOT EXISTS ledger.counterparty (
    counterparty_id  text PRIMARY KEY,
    legal_name       text NOT NULL,
    entity_type      text,                          -- vendor | customer | partner
    country          char(2),
    risk_rating      text,                          -- low | medium | high | unknown
    is_active        boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS ledger.contract (
    contract_id       text        PRIMARY KEY,
    counterparty_id   text        NOT NULL REFERENCES ledger.counterparty (counterparty_id),
    title             text        NOT NULL,
    contract_type     text        NOT NULL,         -- goods | service | lease | nda | employment
    effective_date    date        NOT NULL,
    expiry_date       date        NOT NULL,
    auto_renew        boolean     NOT NULL DEFAULT false,
    notice_days       integer     NOT NULL,         -- days before expiry that notice must be given
    renewal_term_days integer,                      -- NULL when the contract does not renew
    total_amount      numeric(16,2),
    currency          char(3),
    status            text        NOT NULL,         -- active | terminated | expired | renewed
    owner_dept        text        NOT NULL,
    source_system     text        NOT NULL,         -- which system of record asserts this

    CONSTRAINT contract_date_order CHECK (expiry_date >= effective_date),
    CONSTRAINT contract_notice_sane CHECK (notice_days >= 0),
    CONSTRAINT contract_status_closed CHECK (
        status IN ('active', 'terminated', 'expired', 'renewed')
    )
);

CREATE INDEX IF NOT EXISTS contract_expiry_idx ON ledger.contract (expiry_date)
    WHERE status = 'active';

-- The read surface. The reader role is granted here and never on a base table,
-- so a base table added later is not silently reachable. The view's predicate
-- (`status = 'active'`) is the scope: a row the caller may not see is not
-- returned by the database, not filtered out by the application.
--
-- notice_by is computed here rather than in the query so the derived deadline
-- is in-band and the caller never does date arithmetic on a business rule.
CREATE SCHEMA IF NOT EXISTS ledger_ro;

CREATE OR REPLACE VIEW ledger_ro.contract_expiry AS
    SELECT c.contract_id,
           c.title,
           c.contract_type,
           p.legal_name       AS counterparty,
           p.risk_rating,
           c.effective_date,
           c.expiry_date,
           c.auto_renew,
           c.notice_days,
           c.expiry_date - (c.notice_days || ' days')::interval AS notice_by,
           c.total_amount,
           c.currency,
           c.owner_dept,
           c.source_system
      FROM ledger.contract c
      JOIN ledger.counterparty p USING (counterparty_id)
     WHERE c.status = 'active';
