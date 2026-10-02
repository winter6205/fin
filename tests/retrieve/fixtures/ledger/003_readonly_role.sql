-- The read-only reader role. Split from 001_schema.sql because it is the one
-- step in the fixture that needs an explicit authorization: it creates a login
-- and grants it database privileges.
--
-- Why it cannot be run as the admin: the acceptance for this boundary is that a
-- write FAILS. Run with the admin credential, every probe succeeds and the
-- suite is green while proving nothing. Only a role that genuinely lacks the
-- privilege can have that privilege verified.
--
-- The password is development-only, is substituted at run time from the
-- environment, and never appears in a committed file.
--
-- Grants are on NAMED VIEWS, never ALL TABLES: ALL TABLES also covers base
-- tables that land in the schema later, silently widening the blast radius of a
-- leaked credential (spec §2.2, §5a).

\set reader_password `echo "$FIN_SQL_READER_PASSWORD"`

CREATE ROLE fin_sql_reader LOGIN PASSWORD :'reader_password';
GRANT CONNECT ON DATABASE fin_business TO fin_sql_reader;
GRANT USAGE  ON SCHEMA ledger_ro TO fin_sql_reader;
GRANT SELECT ON ledger_ro.contract_expiry TO fin_sql_reader;

-- Belt and braces. default_transaction_read_only is the only layer that refuses
-- a CREATE TEMP TABLE, which is precisely the probe that proves this layer took
-- effect: a temp table is otherwise permitted on a read-only-ish grant shape.
ALTER ROLE fin_sql_reader SET default_transaction_read_only = on;
ALTER ROLE fin_sql_reader SET statement_timeout = '3s';
ALTER ROLE fin_sql_reader SET lock_timeout = '1s';
ALTER ROLE fin_sql_reader SET idle_in_transaction_session_timeout = '10s';
