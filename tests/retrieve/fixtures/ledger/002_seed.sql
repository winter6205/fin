-- Seed rows for the synthetic ledger. Expected values for acceptance are
-- asserted against the literals in this file — a test must not read them back
-- out of the database, or a broken seed would make it pass vacuously.
--
-- Dates are fixed rather than relative to now(), so a reviewer's expectation is
-- stable and a test can state its window explicitly. Tests query the windows
-- below; they do not compute "next 90 days" from the clock.

INSERT INTO ledger.counterparty (counterparty_id, legal_name, entity_type, country, risk_rating, is_active) VALUES
    ('CP-001', 'Northwind Logistics',      'vendor',   'US', 'low',    true),
    ('CP-002', 'Ardent Facility Services', 'vendor',   'GB', 'medium', true),
    ('CP-003', 'Meridian Analytics GmbH',   'partner',  'DE', 'high',   true),
    ('CP-004', 'Halcyon Print Works',      'vendor',   'US', 'unknown',false);

INSERT INTO ledger.contract (contract_id, counterparty_id, title, contract_type,
                            effective_date, expiry_date, auto_renew, notice_days,
                            renewal_term_days, total_amount, currency, status,
                            owner_dept, source_system) VALUES
    -- Inside the 2026-Q1 window. The canonical hit: notice arithmetic is the
    -- point of this row (30 days before 2026-03-14 is 2026-02-12).
    ('C-2291', 'CP-001', 'Warehouse logistics master agreement', 'service',
     '2024-03-15', '2026-03-14', true,  30, 365, 480000.00, 'USD', 'active',
     'operations', 'clm'),

    -- Same window, a counterparty name that the filing citation path would
    -- never see. Kept deliberately: it is the row that proves the sql 档 is not
    -- routed through a document citation, and the regression guard for anyone
    -- who later reuses the document evidence shape for it.
    ('C-2302', 'CP-003', 'Quarterly analytics platform licence', 'service',
     '2025-01-01', '2026-02-28', false, 60, NULL, 96000.50, 'EUR', 'active',
     'finance', 'procurement'),

    -- Later in the year: proves the window bound is a bound and not "everything".
    ('C-2310', 'CP-002', 'Facilities maintenance contract', 'service',
     '2025-07-01', '2026-09-30', true,  45, 180, 250000.00, 'GBP', 'active',
     'facilities', 'clm'),

    -- Not auto-renewing and with a NULL renewal term: the two fields that are
    -- genuinely optional, so a template that assumes they are populated fails.
    ('C-2315', 'CP-001', 'NDA — counterparty onboarding', 'nda',
     '2026-01-10', '2026-01-24', false,  7, NULL, NULL,   NULL, 'active',
     'legal', 'clm'),

    -- Terminated: invisible through the view, present in the base table. This is
    -- the row that distinguishes the view layer from an application filter.
    ('C-2320', 'CP-001', 'Pilot shipping contract', 'goods',
     '2026-02-01', '2026-03-20', false, 14, NULL, 15000.00, 'USD', 'terminated',
     'operations', 'clm'),

    -- A record key shaped like a filing locator. Under the document citation
    -- path this spelling is read as a forged locator and refuses the whole run;
    -- the sql 档 must treat it as an ordinary ledger value. Present so that
    -- property is asserted rather than assumed.
    ('C-2024Q1', 'CP-004', 'Print collateral retainer', 'goods',
     '2026-03-01', '2026-04-30', false, 30, NULL, 8000.00, 'USD', 'active',
     'marketing', 'clm');

-- Row counts a reviewer can check by hand: 4 counterparties, 6 contracts, of
-- which 5 are active and therefore 5 visible through ledger_ro.contract_expiry.
