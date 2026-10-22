# iknow config and retrieval window

`scripts/dev-up.sh` no longer exports `GRAPHRAG_MEMORY_*` and no longer copies `FIN_API_KEY` into `ANTHROPIC_AUTH_TOKEN`. iknow configuration is `~/.iknow-memory/setting.json`. The MCP server should start the iknow MCP through iknow's own config load, not by sourcing the iknow checkout `.env` files.

When `iknow-memory-postgres`, `iknow-memory-neo4j`, and `iknow-memory-es` are all running, dev-up does not start iknow compose. Otherwise it runs `docker compose -p iknow-memory -f ~/.iknow-memory/compose/docker-compose.production.yml`. If that file is missing, dev-up exits non-zero and the message names that path. This is the default and stays the default.

## Choosing the iknow stack

`FIN_IKNOW_STACK=dev` selects the isolated demo stack instead of the production one:

- compose file: the iknow checkout's `docker-compose.yml`, under `$IKNOW_ROOT` (`FIN_IKNOW_ROOT` when set);
- compose project: `iknow-memory-dev`, publishing PG 5434, ES 9201, Neo4j 7688;
- "already running" probe: `iknow-memory-dev-postgres`, `iknow-memory-dev-neo4j`, `iknow-memory-dev-es` — the production names above are not consulted, so a running production stack no longer satisfies the probe for the dev one;
- missing compose file: dev-up exits non-zero and the message names that path, the same failure the production template gets.

Any other value exits non-zero, before the SPA or iknow build starts. The selector is an environment variable rather than a flag because `fin up` execs dev-up.sh without arguments; `~/.fin/settings.yaml` cannot supply it either.

```sh
FIN_IKNOW_STACK=dev fin up     # bring up the isolated demo stack
FIN_IKNOW_STACK=dev fin check  # preflight only; prints the selected stack
```

Bringing the demo stack up does not by itself point retrieval at it. `IKNOW_MEMORY_HOME` is what names the stack iknow-memory reads, including its ports; it is inherited by the MCP child, and `dev-up.sh` does not set it. Export it yourself, pointing at the same stack (5434 / 9201 / 7688).

Eval windows should use `retrieval_fingerprint`. `generation_id` is whatever iknow returned; it may still be unread until iknow reads the embedding generation. The run payload field is `retrieval_window`.

`fin down` still stops only the iknow checkout dev compose — project `iknow-memory-dev`, which is the stack `FIN_IKNOW_STACK=dev` starts. It does not stop the production compose project `iknow-memory`. That is intentional: `fin down` must not stop the operator's production containers.
