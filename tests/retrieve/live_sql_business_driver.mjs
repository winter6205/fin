/**
 * Live driver for the §9 leg-2 acceptance (fin issue #322 A3a).
 *
 * Drives the REAL iknow `BusinessQueryExecutor` build (dist, branch
 * `sql-business-query-module`) against the real PostgreSQL dev instance on
 * port 5434 with the read-only `fin_sql_reader` credential. It exists so the
 * opt-in pytest file can assert executor behaviour (named templates, refusal
 * before connection, failure tokens, `queried_at`) at the actual database
 * boundary without embedding Node logic in Python.
 *
 * Seam covered: iknow business-query executor -> pg pool -> PostgreSQL.
 * Seam NOT covered: fin's host/port/MCP retrieve stack above the executor
 * (owned by the concurrent T7 wiring) and the operator browser path (B3).
 *
 * The credential is read from the environment (`FIN_SQL_READER_URL`, or
 * `FIN_SQL_PROBE_URL` for the deliberately-unreachable probe) and is never
 * printed: stdout carries only the sanitized JSON result.
 */
// Dynamic import: the dist path is env-selected (the merge-window branch build
// vs main), so it cannot be a static module specifier.
const { BusinessQueryExecutor } = await import(
  process.env.IKNOW_DIST_ROOT + "/core/business-query/sql-query-executor.js"
);

const action = process.argv[2];
if (action === undefined) {
  console.error("usage: live_sql_business_driver.mjs <action>");
  process.exit(2);
}

const SCOPES = ["ledger_ro.contract_expiry"];

function executorFor(connectionString) {
  return new BusinessQueryExecutor({
    connectionString,
    authorizedScopes: SCOPES,
  });
}

function errorPayload(error) {
  return {
    kind: "error",
    token: error.failureToken ?? null,
    name: error.name,
    message: error.message,
  };
}

async function run(query) {
  const executor = executorFor(process.env.FIN_SQL_READER_URL);
  try {
    const result = await executor.query(query);
    console.log(JSON.stringify({ kind: "result", ...result }));
  } catch (error) {
    // An observed refusal is a legitimate outcome the pytest asserts on, so
    // the driver still exits 0; the JSON `kind` carries the distinction.
    console.log(JSON.stringify(errorPayload(error)));
  } finally {
    await executor.close();
  }
}

switch (action) {
  // The fixed contracts_expiring template, 2026 window, live reader role.
  case "window":
    await run({
      template: "contracts_expiring",
      params: { windowStart: "2026-01-01", windowEnd: "2026-04-30", limit: 20 },
    });
    break;
  // A window with no seeded contract inside it: the legitimate empty outcome.
  case "empty":
    await run({
      template: "contracts_expiring",
      params: { windowStart: "2020-01-01", windowEnd: "2020-12-31", limit: 20 },
    });
    break;
  // A template name outside the closed set, asked of an UNREACHABLE instance:
  // if the refusal token comes back (not an unavailability), the request was
  // refused before any connection existed — which is what §5(c) promises.
  case "unknown-template":
  // A recognized template against the same unreachable instance: proves the
  // empty-window exit and an unavailability are different observed tokens.
  case "unreachable":
    {
      const executor = executorFor(process.env.FIN_SQL_PROBE_URL);
      const query =
        action === "unknown-template"
          ? {
              template: "free_form_probe",
              params: { windowStart: "2026-01-01", windowEnd: "2026-04-30", limit: 20 },
            }
          : {
              template: "contracts_expiring",
              params: { windowStart: "2026-01-01", windowEnd: "2026-04-30", limit: 20 },
            };
      try {
        const result = await executor.query(query);
        console.log(JSON.stringify({ kind: "result", unexpected: true, result }));
        process.exitCode = 1;
      } catch (error) {
        console.log(JSON.stringify(errorPayload(error)));
      } finally {
        await executor.close();
      }
    }
    break;
  default:
    console.error(`unknown driver action: ${action}`);
    process.exit(2);
}
