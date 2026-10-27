## Cross-Project Code Quality Patterns (Argus + Pythia + Tablina)

Deep codebase reviews across three production projects surfaced recurring patterns. Apply these as a checklist when writing or reviewing code.

### DRY violations that recur in every project

- **Time helpers** (`_now()`, `_iso()`, `_expired()`) — duplicated across auth, service, oauth, and db modules. Extract to `core/_timeutil.py`. If `grep -r "datetime.now(UTC).isoformat()"` returns more than 1 hit, flag for extraction.
- **Provider retry loops** — each LLM/embedding provider independently implements `for attempt in range(3): await asyncio.sleep(2 ** attempt)`. Extract `_post_with_retries` once, reuse across all providers.
- **Atomic-claim blocks** — `BEGIN IMMEDIATE` → check status → raise → set meta → commit logic copy-pasted for reindex/rechunk/purge. Extract `_claim_lock(conn, operation, resume)`.

### Architectural smells that recur

- **God files** — every project has one: `routes.py` (1257 lines), `oauth.py` (1190 lines), `db.py` (1871 lines). Apply the Rule of 500 (see §4). Split by responsibility, not by arbitrary line count.
- **Config sprawl** — scattered `os.getenv` calls (66 in Pythia, 0 centralization). Centralize into a pydantic `Settings` model. Tablina's `config.py` is the reference: layered precedence (DEFAULTS → yaml → DB → env), only orchestration keys env-overridable.
- **Module-level singletons** — creating heavy objects (ModelManager, engine) at import time wastes resources and makes testing harder. Use lazy-load via factory or `lru_cache`.
- **O(N) list scans for single-record lookups** — `next((x for x in list_all(engine) if x["id"] == id), None)` loads the full table. Use `s.get(Model, id)` for O(1).
- **In-memory filtering when FTS5 exists** — if a full-text search table exists, API endpoints should use it, not load all records and Python-filter.
- **Boolean string parsing repeated** — `"0"/"false"/"off"/"no"` parsed in 4+ places. Extract `parse_bool` helper.

### Security patterns that recur (what reviewers catch)

- **OAuth/DCR validation gaps** — every project had one (Argus: `javascript:` URIs, Pythia: scalar coercion, Tablina: admin scope bypass). Codex catches these; Hermes verification doesn't.
- **SSRF / perimeter defense** — Opus exclusively catches these. If the project has MCP servers, URL crawlers, or admin routes, Opus must review them.
- **Concurrency: `BEGIN IMMEDIATE`** — single session ≠ serialized writes. Both Argus and Tablina needed this fix.
- **Malformed JSON → 500** — routes that accept JSON bodies should return 400, not 500, on malformed input.

### Best practices to adopt cross-project

- **Tablina's degradation philosophy**: "never abort indexing" + monotonic deadline budgets. Each failure path has a documented lenient default.
- **Tablina's test docstrings**: explain the **hazard being defended**, not just the behavior.
- **Pythia's schema-as-source-of-truth**: `core/schema.py` shared across API, MCP, and CLI.
- **Tablina's config precedence**: DEFAULTS → yaml → DB → env, only orchestration keys env-overridable.
- **Pythia's Protocol-based backend abstraction**: `ForecastBackend` Protocol with pluggable implementations.
