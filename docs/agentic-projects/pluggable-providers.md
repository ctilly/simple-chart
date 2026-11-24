# Pluggable data providers

Status: Approved (2026-11-23); complete
Version: 0.3
Owner objective: Any data source can be plugged into SimpleChart and work,
with no edits under `app/` and no path that leaks credentials.
Approval profile: Gated autonomy
Current graph phase: Accepted by the user, 2026-11-24 (manual cross-connection data check looked good)
Last updated: 2026-11-23

## 1. Resumption state

```text
Last completed node: Codex design QC adjudicated (PP-QC-01..12); spec 0.2
Active node: none
Active writer: none
Files owned by active writer: none
Pending join or gate: user final acceptance
Verified evidence available: §3 inventory; PP-QC-04 and PP-QC-07 spot-checked
Known unrelated workspace changes: the accepted background-jobs project is
  uncommitted in the working tree; docs/prompts/ is the user's
Exact next transition: user final acceptance
```

Roles: Claude Code orchestrates, owns this spec, adjudicates findings, and
does code QC after each slice. Codex (`gpt-6.2-sol`, high effort) is the
single writer and gave the design QC. Claude has final say.

Autonomy envelope:

```text
Approval profile: Gated autonomy
Approved objective: §2 and §4 (pending approval)
In-scope layers: data/provider/, data/cache.py + schema, the new providers/
  package, a startup bootstrap, app/ routing and settings, main.py,
  simplechart/plugins.py, tests, docs/skills/data-provider.md, AGENTS.md,
  docs/credential-security.md (additions only), agent skill forwarders
Allowed writes: files named per slice in §10, plus this artifact
Allowed verification: focused offscreen pytest, full suite at each slice
  end, ruff, mypy; in-memory fake keyrings and temporary SQLite files only
Allowed external actions and data: Codex CLI receives repo paths and this
  spec. No real credentials, no user database, no OS keyring writes, no live
  provider calls in tests
Prohibited: git or source control, new dependencies, weakening any rule in
  docs/credential-security.md, edits outside §10
Mandatory user gates: spec approval, material amendments, final acceptance
Required reviewers: Claude code QC after each slice
Stop conditions: a test must be weakened; a credential rule would change;
  a user-visible change not listed in §5; two failed hypotheses on a defect
Reporting cadence: a short evidence update after each slice
```

## 2. User objective

"The goal of a data interface should be to make it so that we can plug any
data source in and have it work with Simple Chart." Others will have their
own agents add data sources. Security comes first: "we don't want an
architecture that leaks credentials."

Decided by the user, 2026-11-23:

- Sources may be mixed. The selected provider supplies what it supports, and
  Yahoo supplies the rest (example: Alpaca bars with Yahoo company data).
- Alpha Vantage is only an example. No new real provider is added.
- Claude chooses the extension mechanism, security first (§6.1).

## 3. Problem statement (Observed in source, 2026-11-23)

The `DataProvider` interface exists, but Yahoo and Alpaca knowledge is
hard-coded around it:

| Where | Hard-coded knowledge |
| --- | --- |
| `data/provider/config.py` | three fixed connection IDs; Alpaca `environment`/`feed` on every `ProviderConnection`; `cache_namespace` branches |
| `data/provider/credentials.py` | `ProviderCredentials` is always `api_key_id` + `api_secret`; JSON decoded outside the error handler |
| `data/provider/__init__.py` | `_registry`/`_requirements` list both factories by hand |
| `data/provider/base.py` | `fetch_bars`, `fetch_snapshots`, `fetch_level1`, `native_timeframes` are abstract, so every source must implement quotes |
| `data/cache.py` | seeds `fixed_connections()`; `set_provider_connection_feed` is Alpaca-only; `environment`/`feed` columns; legacy bars SQL default `'yfinance'` (line 142) |
| `app/controller.py` | `YFINANCE_CONNECTION_ID` fallback; `_is_yahoo_futures_symbol`; reference data pinned to Yahoo; activation probe `fetch_level1("SPY")` (`_validate_provider_connection`); `load_plugins()` at module import |
| `app/application_settings.py` | `AlpacaConnectionDialog`; `provider_name == "alpaca"` branches |
| `app/bar_comparison.py`, `app/data_quality.py` | Alpaca feed variants, `"alpaca:*"` literals, `_SOURCE_LABELS` |
| `app/symbol_details.py`, `app/header_bar.py` | "Yahoo" attribution on profile, news and earnings tooltip |
| `app/data_source_status.py`, `app/extension_runtime.py` | `"yfinance"` defaults |
| `main.py` | `--provider` choices listed by hand |
| tests | helpers importing provider modules (`tests/realistic_errors.py`), the global Yahoo stub (`tests/conftest.py`), registry monkeypatches (`tests/test_provider_config.py`) |

Carried forward from background-jobs QC:

- **S4-QC-03:** company-name precedence and the symbol-only
  `asset_reference` row can mix sources after a switch.
- **S4-QC-04:** news timestamp normalization is left to Yahoo's parser.

Found in this QC (**PP-QC-09**, Observed): profile/news, earnings and
company-name retries skip reloads for the same symbol, so a provider switch
can keep showing the previous source's data.

## 4. Goals and non-goals

### Goals

1. **One declaration per source.** A provider is one package under
   `providers/` exposing a module-level `PROVIDER: ProviderSpec`.
2. **No source names outside providers.** Nothing in `app/`, `data/`
   (outside the generic registry), `simplechart/` or `main.py` names Yahoo or
   Alpaca, except the one fallback constant (§6.3).
3. **Credential isolation by construction** (§6.4).
4. **Per-capability routing** with Yahoo as the fallback for anything the
   selected provider doesn't declare.
5. **Agent-ready:** a `docs/skills/data-provider.md` skill plus an
   executable conformance test any new provider must pass.

### Non-goals

- No new real provider; no new Alpaca features.
- No providers from `~/.simplechart/plugins` (§6.1).
- No sandboxing; §6.1 states the trust boundary honestly.
- No change to the six controller workers' threading.
- No credential migration: existing keyring entries keep working unchanged.

## 5. User-visible behavior

Unchanged unless listed:

- Settings shows the same three connections and columns. The Alpaca dialog
  shows the same fields (feed, key ID, secret) with today's behavior: stored
  values show as a placeholder, blank keeps the stored pair, entering both
  replaces it, inputs are cleared on Save or Cancel.
- The chosen Alpaca feed and the active connection survive the upgrade.
- Attribution text ("Yahoo") on profile, news and the earnings tooltip names
  the provider that actually served the data. Today it is always Yahoo, so
  nothing changes visibly until a provider declares those capabilities.
- **Change 1:** Compare Bars with a real-time SIP origin gains a "SIP
  (15-minute delayed)" corroboration row (rule: every other comparable
  namespace, §6.6).
- **Change 2:** switching providers on the same symbol reloads the details
  window, earnings and company name from the newly routed source
  (PP-QC-09 fix).
- A provider package that fails to load or validate is skipped with the
  existing fixed plugin notice. If Yahoo itself fails, startup fails with a
  fixed error, as it does today when Yahoo is unavailable.

## 6. Design

### 6.1 Where providers come from (security decision)

- Providers are packages under a top-level `providers/` package. Built-in
  Yahoo and Alpaca move there (`providers/yahoo/`, `providers/alpaca/`), so
  agents copy a real exemplar.
- **Declarative registration (PP-QC-05):** a provider package exposes
  `PROVIDER: ProviderSpec`. Discovery imports each package, reads that
  attribute, validates it, and only then adds it. There is no public
  `register_provider` function, so an import that raises halfway leaves
  nothing registered.
- After discovery the registry is built once as an immutable mapping
  (`MappingProxyType`, tuples, frozen dataclasses with no mutable fields)
  and sealed. Nothing later in startup can add a provider.
- **Explicit bootstrap order (PP-QC-11):** `simplechart/bootstrap.py`
  runs: output safety → provider discovery and seal → extension plugins
  (`load_plugins`) → cache → window. `load_plugins()` is removed from
  `app/controller.py`'s module import. `main.py` and test construction paths
  both go through the bootstrap; tests can pass a fixture provider package
  to the production discovery function.
- Providers are never loaded from `~/.simplechart/plugins`. User plugins keep
  receiving no credential store, provider or connection object through
  `simplechart.api`.
- **Honest limit** (added to the skill and to credential-security.md): any
  module in the process can import `keyring` directly. Sealing and
  declarative registration protect the supported app paths from accidental
  or casual misuse. They aren't a sandbox. The trust boundary is "code in
  your project tree and plugin folder is trusted".

### 6.2 The declaration (`data/provider/spec.py`)

```python
@dataclass(frozen=True)
class CredentialField:          # value stored only in the OS keyring
    key: str                    # [a-z][a-z0-9_]*
    label: str

@dataclass(frozen=True)
class OptionField:              # non-secret, stored in SQLite
    key: str
    label: str
    choices: tuple[tuple[str, str], ...]   # (value, label): choices only
    default: str
    user_editable: bool = True

@dataclass(frozen=True)
class ConnectionTemplate:
    connection_id: str          # == provider name, or "<name>-<suffix>"
    display_name: str
    fixed_options: tuple[tuple[str, str], ...] = ()   # e.g. environment=paper

@dataclass(frozen=True)
class NamespaceSpec:            # PP-QC-08
    namespace: str              # "<name>" or "<name>:<suffix>"
    label: str
    options: tuple[tuple[str, str], ...]   # option values selecting it
    comparable: bool            # offered as a Compare Bars source

class Capability(StrEnum):
    BARS, SNAPSHOTS, LEVEL1, COMPANY_NAME, PROFILE, NEWS, SEARCH,
    UPCOMING_EARNINGS, PAST_EARNINGS, DIVIDENDS

@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display_name: str
    connections: tuple[ConnectionTemplate, ...]
    capabilities: frozenset[Capability]
    namespaces: tuple[NamespaceSpec, ...]
    factory: Callable[[ProviderConnection, ProviderCredentials | None], DataProvider]
    credential_fields: tuple[CredentialField, ...] = ()
    option_fields: tuple[OptionField, ...] = ()
    required_modules: tuple[tuple[str, str], ...] = ()
    serves_symbol: Callable[[str], bool] = _all_symbols
```

Validation at discovery (a violation skips the provider with the notice):

- `requires_credentials` is derived: `bool(credential_fields)`.
- Option values are choices only. Free text has no non-secret path.
- Connection IDs are unique across providers, prefixed by provider name,
  and may not start with the keyring probe prefix
  (`availability-probe-`). Namespaces are unique across providers and
  prefixed by provider name, so cached bars and corrections can't collide.
- Every option combination a connection can hold resolves to exactly one
  `NamespaceSpec`. A provider with one namespace declares it with no options.
- Every declared capability's `DataProvider` method is overridden by the
  factory's class (checked by the conformance test, AC9).
- Factory imports stay lazy (credential-security rule 3).

`ProviderConnection` becomes `connection_id, display_name, provider_name,
options` (an immutable tuple of pairs). `cache_namespace` is resolved by the
registry. Alpaca's `MarketDataFeed` and `ConnectionEnvironment` move into
`providers/alpaca/`.

`DataProvider` (PP-QC-07): the quote and bar methods stop being abstract.
Their default raises `UnsupportedCapabilityError` (never reached, because
routing checks capabilities first). A new `check_connection()` is the
provider-owned activation probe used by Settings; its default raises.
Alpaca and Yahoo implement it as today's `fetch_level1("SPY")` check.

### 6.3 Capabilities and routing

- The capability set is the routing truth. An undeclared capability is never
  requested from the selected provider. "Declared but empty" shows empty;
  "not declared" falls back.
- `MainWindow` holds a `CapabilityRouter(selected_route, fallback_route)`.
  `router.route(capability, symbol)` returns the selected route when its spec
  declares the capability and `serves_symbol(symbol)`; otherwise the
  fallback route. It replaces `_is_yahoo_futures_symbol` (Alpaca's
  `serves_symbol` rejects `=F` symbols) and `_reference_aggregator`.
- `FALLBACK_PROVIDER = "yfinance"` in `data/provider/registry.py` is the one
  allowed source name outside providers. At seal time the fallback must be
  present, valid, credential-free, and declare every capability; otherwise
  startup fails with a fixed error.
- **PP-QC-09:** components resolve their route at submission, keyed by
  (route identity, symbol). A route change invalidates cached results and
  in-flight jobs (via `LatestJob` generations) and resubmits for the open
  symbol: details (profile and news separately), `EarningsFeed`, and
  company-name retries, which are keyed by connection.
- **S4-QC-03:** company name follows `COMPANY_NAME` routing.
  `asset_reference` gains a `source` column (provider name); a row from
  another source is a cache miss.
- **S4-QC-04:** `NewsArticle.published` is timezone-aware UTC or None.
  Providers normalize; the app converts for display. AC9 enforces it.
- Attribution: profile, news and earnings labels use the serving provider's
  `display_name`.
- Today's providers keep today's behavior: Alpaca declares BARS, SNAPSHOTS
  and LEVEL1 only.

### 6.4 Credentials

- **Ownership (PP-QC-01):** every credential-store operation goes through
  the registry: `credentials_for(connection_id)` looks up which registered
  template owns that ID, reads that entry, and passes it only to that
  template's provider factory. Callers never pass a provider name with a
  connection ID, so a mismatched pair can't be constructed. Compare Bars
  receives namespaces, not connections (§6.6).
- **Payload (PP-QC-02):** the keyring payload stays today's JSON object, so
  `{"api_key_id", "api_secret"}` entries load unchanged. Decoding requires
  a JSON object whose keys exactly equal the declared fields, with no
  duplicates, and whose values are non-blank strings. All decode failures,
  including malformed JSON, become one fixed `CredentialStoreUnavailableError`
  ("Stored credentials for {display name} are invalid."). No value or key
  text is echoed.
- `ProviderCredentials` is a frozen object with `get(key) -> str`, a
  redacting `repr`/`str`, and no iteration, `items()` or equality that
  exposes values.
- **Dialog (PP-QC-03):** one masked input per `CredentialField`, one combo
  per user-editable `OptionField`. Save order: validate all inputs, write
  options to SQLite (non-secret, harmless if the next step fails), then
  `CredentialStore.put`. A keyring failure shows the fixed error and leaves
  the old credential entry intact. Inputs are cleared on Save and Cancel.
- The startup secure-store probe and availability rules are unchanged.

### 6.5 Storage (PP-QC-10)

- `provider_connections` gains `options TEXT NOT NULL DEFAULT '{}'`
  (JSON of option key to choice value). The migration runs in one
  transaction and is idempotent: it copies `environment`/`feed` into
  `options` only when the legacy values are valid choices for the declared
  fields, else uses the declared default. Old columns stay, unused.
- Seeding uses the sealed registry's templates. **Rows for providers that
  are missing in this run are kept**, not deleted, and hidden from Settings,
  so a provider that fails to load once doesn't lose its settings. The
  active connection falls back to Yahoo for the session only; the stored
  active ID is kept until the user picks another.
- `set_provider_connection_feed` becomes `set_provider_connection_options`,
  validated against the declared choices.
- Cache namespaces are unchanged (`yfinance`, `alpaca:<feed>`), so cached
  bars and corrections carry over.
- `main.py --provider` accepts any registered connection ID, validated
  after discovery.

### 6.6 Labels and Compare Bars

- `data_quality`, `bar_comparison` and attribution get labels from
  `registry.namespace_label(namespace)` and the spec's `display_name`.
- Compare Bars: the origin row is fetched through the provider owning the
  origin namespace (any namespace, including real-time SIP). Corroboration
  rows: every other `comparable` namespace from every available provider
  with configured credentials (one connection per provider: the active
  connection first, then template order). The registry builds the
  connection variant from the namespace's options. Alpaca marks IEX and
  delayed SIP comparable; Yahoo's single namespace is comparable.

### 6.7 Error output (PP-QC-04)

- `providers/` is added to `tests/test_no_raw_exception_output.py` and to
  `simplechart/output_safety.py`'s trusted-package list in P2.
- Providers may raise only the `data/provider/base.py` error types and may
  not subclass `UserFacingError`. When a `UserFacingError` base type takes a message, the
  provider's message must be a string literal, or an f-string whose only
  interpolations are a `display_name` name or attribute. No exception text,
  response body, or other outside value can reach the UI. An AST check in
  AC9 enforces it under `providers/` (amended in 0.3).

## 8. Acceptance criteria

- **AC1, zero-edit plug-in:** a test fixture package
  (`tests/fixtures/providers/fakesource/`) with one credential field
  (`token`), one option, and capabilities {BARS, PROFILE}, passed to the
  production discovery function, works end to end with no edit under
  `app/`. It appears in Settings; the dialog shows one masked input;
  saving writes only the in-memory test keyring; activation through Settings
  passes its `check_connection()`; bars and profile come from it, labelled
  with its name; news, search and earnings come from Yahoo.
- **AC2, credential scoping:** with two credential-bearing fake providers
  whose field schemas match, each factory receives only its own entry;
  there is no API that accepts a (provider, connection ID) pair.
- **AC3, no credential leaks:** after AC1's flow and after a forced keyring
  failure, the synthetic secret is absent from the SQLite database, `-wal`
  and `-journal` files, captured logs, and the `repr` of every credential
  object. A fake provider that raises with an unlabelled secret sentinel
  shows only the fixed message.
- **AC4, sealing:** the registry can't be added to after discovery; a
  `~/.simplechart/plugins` file can't add a provider through supported
  paths; a provider package that raises after building its spec registers
  nothing.
- **AC5, validation:** free-text options, duplicate or unprefixed connection
  IDs or namespaces, a probe-prefixed ID, or an option combination with no
  namespace are rejected with a notice while the app starts with Yahoo. An
  invalid fallback fails startup with the fixed error.
- **AC6, compatibility:** a database and keyring created by today's schema
  open with the same connections, Alpaca feed, active connection and
  working credentials; upgrading twice is a no-op; a provider skipped in one
  run and restored in the next gets back its row and keyring entry; an
  invalid legacy feed becomes the declared default.
- **AC7, routing:** an undeclared capability never calls the selected
  provider; a declared one never calls the fallback; `=F` symbols route to
  Yahoo under Alpaca; a same-symbol provider switch with requests in flight
  shows only the new route's results.
- **AC8, boundary:** an AST test fails if `app/`, `data/` (outside
  `data/provider/registry.py`), `simplechart/` or `main.py` imports
  `providers.*` or contains `"alpaca"`/`"yfinance"` literals. Enabled in
  full in P6, after the last caller migrates.
- **AC9, conformance:** a parametrized test runs every discovered spec
  through the declaration rules, the capability-override check, the news
  timestamp contract, and the provider error-text rule.
- **AC10, skill:** `docs/skills/data-provider.md` exists, AGENTS.md links it,
  and the fixture provider follows it.

## 9. TDD test matrix

Each slice adds its own behavioral RED tests and goes GREEN without
weakening existing assertions. Existing tests may change only where they
name moved symbols or construct `MainWindow` (bootstrap path); each edit is
listed in the slice report.

## 10. Implementation slices (PP-QC-12)

Each slice is GREEN on its own. Producer and consumer changes land
together, or a temporary adapter keeps the old API working until the slice
that removes it.

| Slice | Content | Writes |
| --- | --- | --- |
| P1 | Spec model, credential wrapper and decoding, declarative discovery, validation, sealing, `credentials_for`; Yahoo/Alpaca `PROVIDER` specs in their current modules; old `config.py`/`create_provider` API kept as adapters over the registry | `data/provider/**`, tests |
| P2 | `providers/` package and move; `simplechart/bootstrap.py`; `load_plugins` out of controller import; raw-output scan and output safety include `providers/`; test helper import updates | `providers/**`, `simplechart/{bootstrap,plugins,output_safety}.py`, `main.py`, `app/controller.py` (bootstrap only), `pyproject.toml` packages list, `tests/**` helpers |
| P3 | Connections and settings end to end: `ProviderConnection.options`, schema migration, seeding that keeps missing rows, option setter, generic connection dialog, `--provider`; remove the `config.py` adapters | `data/cache.py`, `data/schema.sql`, `data/provider/**`, `app/application_settings.py`, `main.py`, tests |
| P4 | `DataProvider` defaults and `check_connection()`; capability router; route-change invalidation (PP-QC-09); company-name source; news contract; attribution | `data/provider/base.py`, `app/controller.py`, `app/symbol_details.py`, `app/earnings.py`, `app/header_bar.py`, `app/data_source_status.py`, `app/extension_runtime.py`, `data/cache.py`, `providers/**`, tests |
| P5 | Labels and Compare Bars from the registry | `app/bar_comparison.py`, `app/data_quality.py`, tests |
| P6 | Fixture provider and AC1–AC10 tests; full AC8; skill file; AGENTS.md; forwarders; credential-security.md trust-boundary and registration additions | `tests/**`, `docs/skills/data-provider.md`, `.codex/.claude/.gemini/.grok` forwarders, `AGENTS.md`, `docs/credential-security.md` |
| V | Full suite, ruff, mypy; production call sites named | — |

The credential-security.md edit only adds text. Claude checks line by line
that no existing rule is relaxed.

## 11. Risks and falsification checks

- **Hidden source knowledge remains:** AC8, plus
  `grep -rni "alpaca\|yfinance" app data simplechart main.py`.
- **Migration loses settings:** AC6 against a database built by the old
  schema.
- **Bootstrap order regresses:** AC4; a test asserting plugins load after
  sealing.
- **Stale data after a switch:** AC7's in-flight test.
- **mypy/mypyc:** plain frozen dataclasses and callables; no kernels touched.

## 12. Open questions for the user

None blocking. Approval of this spec covers the §6.1 location, bootstrap
and sealing decisions, the two §5 changes, and the §6.5 schema migration.

## 13. QC findings and dispositions

Codex design QC, 2026-11-23. All accepted; dispositions are in the
sections noted.

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| PP-QC-01 | High | Prefixes don't enforce credential ownership; mismatched (provider, ID) pairs | Accepted: `credentials_for` owns the lookup; no pair API; probe prefix reserved (§6.4, AC2) |
| PP-QC-02 | High | Malformed payloads escape handlers; wrapper could expose values | Accepted: strict decode, fixed error, no-iteration wrapper (§6.4) |
| PP-QC-03 | Med | Dialog lifecycle and partial-save failure unspecified; WAL/journal | Accepted (§5, §6.4, AC3) |
| PP-QC-04 | High | `providers/` outside the raw-output scan; provider-authored error text | Accepted (§6.7, AC3, AC9) |
| PP-QC-05 | High | Mutable declarations; partial registration survives a failed import | Accepted: declarative `PROVIDER`, immutable registry (§6.1, AC4) |
| PP-QC-06 | Med | Inventory missed attribution, defaults, legacy SQL, test wiring | Accepted (§3, P2, P4) |
| PP-QC-07 | High | AC1 can't activate through the SPY probe; abstract quote methods | Accepted: `check_connection()`, non-abstract defaults (§6.2) |
| PP-QC-08 | Med | Comparison metadata duplicated; origin resolution open | Accepted: `NamespaceSpec` (§6.2, §6.6) |
| PP-QC-09 | High | Same-symbol switches keep stale reference data | Accepted: route-keyed invalidation (§6.3, §5 change 2, AC7) |
| PP-QC-10 | Med | Upgrade failure and provider return not covered | Accepted: atomic idempotent migration; missing rows kept (§6.5, AC6) |
| PP-QC-11 | High | Plugins load at controller import, before any discovery | Accepted: `simplechart/bootstrap.py` order (§6.1) |
| PP-QC-12 | High | Slices can't each reach GREEN | Accepted: adapters, co-landed producers/consumers, AC8 last (§10) |

## 14. Plan-change log

- 0.1: initial draft, 2026-11-23.
- 0.2: accepted PP-QC-01 to PP-QC-12. Material changes from 0.1:
  declarative `PROVIDER` instead of `register_provider`; explicit bootstrap;
  `NamespaceSpec`; `check_connection()`; route-change invalidation (second
  user-visible change); missing-provider rows kept; slices reordered.
- Approved by the user, 2026-11-23 ("Proceed"). The user asked for
  plainer slice descriptions in reports.
- P1 pause, 2026-11-23 (Codex, Verified): `tests/test_provider_config.py:166`
  assigns to `_registry`, which the sealed registry forbids (`TypeError`
  under `MappingProxyType`). Claude authorized two test-setup edits that keep
  every assertion: supply that test's fake factory through a `PROVIDER`
  declaration before discovery; compare credentials field by field with
  `get(key)` at `tests/test_provider_config.py:245` and
  `tests/test_application_settings.py:272`, because §6.4 forbids value
  equality.
- P1 second pause, 2026-11-23: `tests/test_provider_errors.py:339` activates
  the unregistered ID `alpaca-custom`, which §6.4 ownership now refuses.
  Authorized: use `ALPACA_PAPER_CONNECTION_ID`, keep the display name and
  every assertion. Standing authorization for the rest of the project: an
  existing-test edit is allowed without pausing only when it (a) adapts setup
  to the sealed registry, registered connection IDs, or no value-equality on
  credentials, (b) keeps every assertion, and (c) is listed in the slice
  report. Claude checks each one at QC.
- P1 complete (Codex), Claude QC 2026-11-23. Code read in full:
  `spec.py`, `registry.py`, the credentials diff, both provider
  declarations, the adapters, and all four existing-test edits. The edits
  fit the standing rule; every assertion is kept. Findings:
  - P1-QC-01 (Medium): credentials and the registry depend on each other.
    `KeyringCredentialStore.get/put/delete` call the global
    `default_registry()`, and `credentials_for` uses `isinstance`, then
    calls the private `store._read`. Stores that aren't the keyring skip
    strict decoding. Disposition: fix in P2, once the bootstrap exists. The
    bootstrap builds the registry and passes it in; the `CredentialStore`
    protocol gains a public field-aware read; `default_registry()` is
    removed.
  - P1-QC-02 (Low): compatibility shims. Module `__getattr__` in
    `yfinance_provider.py`, and `_TIMEFRAME_MAP` in `alpaca_provider.py`.
    Alpaca's lazy-import workarounds: `@cache` maps, `TYPE_CHECKING`
    imports. Disposition: P2 splits each provider package into
    `__init__.py` (holding `PROVIDER` only) and an implementation module
    with normal top-level imports, then deletes the shims.
  - P1-QC-03 (Low): the two-argument `ProviderCredentials` constructor and
    the `api_key_id`/`api_secret` properties. Disposition: remove in P3,
    once the dialog is generic and Alpaca's factory uses `get()`.
- P1 verified by Claude: full suite 1484 passed, 1 skipped (Codex's sandbox
  segfault did not reproduce); ruff clean; mypy app/data/chart/simplechart clean.
- P2 complete (Codex), Claude QC 2026-11-23:
  - Verified: full suite 1492 passed, 1 skipped; ruff clean; mypy on app,
    data, chart, simplechart and providers clean; nothing in `app/`, `data/`
    or `main.py` imports `providers`.
  - Assertions: the only removed `assert` lines in `tests/` are the two P1
    field-wise credential edits and two from background-jobs; nothing was
    removed in P2.
  - P1-QC-01 and P1-QC-02 are resolved:
    - The registry is built in `simplechart/bootstrap.py` and passed to
      `MainWindow` and the keyring store.
    - `CredentialStore.read(id, fields, display_name)` is public, with
      strict decoding for every serialized store.
    - The shims are removed. Each provider is split into a declaration in
      `__init__` and an `implementation.py`.
  - The test session runs the real `bootstrap()` once, then restores the
    process hooks.
  - P2-QC-01 (Low): `bootstrap()` repeats the user plugin path default from
    `simplechart/plugins.py`. Disposition: P3 reuses the plugins constant.
- P3 scope note (internal adjustment, 2026-11-23): `ProviderConnection`
  changes shape here. Three later-slice files still read `.feed` and
  `.environment`:
  - `app/controller.py` `_DataRoute.display_name`;
  - `app/bar_comparison.py`;
  - `app/data_quality.py`.

  P3 may make the minimal behavior-preserving edits there so it stays
  GREEN. Their generalization remains P4/P5 work.
- P3 complete (Codex), Claude QC 2026-11-23:
  - Verified: full suite 1502 passed, 1 skipped; ruff and mypy clean.
  - Assertions: 11 removed `assert` lines in `tests/`. Each has an equivalent
    replacement (`.feed` became `options["feed"]`; the dialog object name;
    field-wise credentials).
  - Alpaca names remain only in `app/bar_comparison.py` and
    `app/data_quality.py`, which are P5 work.
  - The migration (one `BEGIN IMMEDIATE` transaction, idempotent, legacy
    values validated against the declared choices) and seeding (missing
    providers' rows kept) match §6.5.
  - P3-QC-01 (Medium): `Cache._row_to_provider_connection` calls
    `build_connection`, which raises `ValueError` on a stored option that is
    no longer a declared choice. A provider update that drops a choice would
    break startup. Disposition: in P4, fall back to the declared default
    for invalid stored options, the same rule the migration uses, and test
    it.
  - P3-QC-02 (Low, user-visible): `ConnectionDialog.accept` clears the
    credential inputs before validating. Typed values are lost on
    "Incomplete Credentials" and on a keyring failure; the old dialog kept
    them. Disposition: in P4, clear only when the dialog closes (a
    successful Save, or Cancel), and test it.
- P4 pause, 2026-11-23: production code done; 11 new tests green. Claude
  authorized:
  - Flip the three P3-written assertions that expect credential inputs to
    clear on a failed save. They now expect the inputs to be kept, per the
    P3-QC-02 disposition.
  - Retarget existing tests that call retired helpers
    (`_is_yahoo_futures_symbol`, `_validate_provider_connection`,
    `_reference_aggregator`, the old routing helper in
    `test_controller_fetch.py`) to `CapabilityRouter` / `check_connection()`,
    with the same expected behavior.

  Any other change to an expected value requires a PAUSE.
- P4 complete (Codex), Claude QC 2026-11-23:
  - Verified: full suite 1513 passed, 1 skipped; ruff clean; mypy (67
    files) clean.
  - Design read: `data/provider/routing.py` (`DataRoute`,
    `CapabilityRouter`), `MainWindow._commit_provider_route`, and
    `SymbolDetails.refresh_routes`. The router is the single decision
    point. A route switch bumps the quote generation and cancels Level 1
    when the quote or name route changes, clears the company name when its
    route changes, refreshes only the details sections whose route
    changed, and reloads, which re-selects earnings.
  - Assertions: the P4 edits rewrite 4 `assert` lines one-for-one
    (`_route_for_symbol` became `router.route(Capability.BARS, …)`;
    `routes.yahoo` became `routes.fallback`), with the same expected values.
    The 3 P3-QC-02 flips are as authorized.
  - P3-QC-01 and P3-QC-02 resolved. The only remaining source literals in
    `app/` are in `bar_comparison.py` and `data_quality.py` (P5).
- P5 complete (Codex), Claude QC 2026-11-23. Verified: full suite 1523
  passed, 1 skipped; ruff clean; mypy clean. Compare Bars builds its sources
  from the registry's comparable namespaces and `connection_variant`. It
  checks availability and credentials per provider. A connection that can
  refresh the origin takes priority. One `assert` was rewritten one-for-one
  (`.feed` became `options["feed"]`). The only source name left in `app/`
  is the `app/controller.py` module docstring; P6 rewords it for AC8.
- P6 pause, 2026-11-23. Claude decided the following.
  - 0.3 internal adjustment to §6.7. The check found four messages
    authored in Alpaca's code:
    - three `MarketDataEntitlementError` messages, which are fixed guidance
      sentences;
    - one `UnsupportedTimeframeError`, which isn't user-facing.

    None can carry outside text, which is the leak §6.7 guards against.
    The rule is narrowed to its purpose: literal text, or text that
    interpolates only `display_name`. Moving Alpaca's feed guidance into
    `base.py` would put provider knowledge back into the framework. This is
    not a credential-policy change.
  - AC8: Codex is authorized to reword the docstrings in `data/cache.py`
    (usage example) and `data/provider/base.py` (module docstring). The
    `FALLBACK_PROVIDER` line in `data/provider/registry.py` stays exempt.
  - Claude wrote `.codex/skills/data-provider.md` (Codex's sandbox can't
    write `.codex/`). Its content is identical to the other three
    forwarders.
- P6 final pause, 2026-11-23: confirmed that the §6.7 rule covers
  `UserFacingError` base types only. `UnsupportedTimeframeError` (a plain
  `Exception`) never reaches the UI as text: `report_error` shows the fixed
  action message, and `describe` logs type names only. Codex's guard
  already used this scope. Four in-memory negative cases prove that the
  check fails on exception text, other names, nested formatting, and
  subclassing.
- V, Claude, 2026-11-23:
  - Full offscreen suite: 1540 passed, 1 skipped. All 177 socket cases that
    Codex's sandbox denied pass here.
  - ruff clean; mypy on app, data, chart, simplechart and providers (67
    files) clean.
  - `grep -rni "alpaca\|yfinance" app data simplechart main.py` finds three
    code comments (`data/errors.py` ×2, `data/aggregator.py`) and the
    allowed `FALLBACK_PROVIDER`. AC8 scans imports and string literals, not
    comments.
  - `docs/credential-security.md`: the diff has no removed or reworded
    lines, only the new "Provider declarations and registration" and
    "In-process trust boundary" sections and the provider error-message
    rule.
  - Skill reviewed: `docs/skills/data-provider.md`, forwarders for all
    four agents, and the AGENTS.md table row and "Where data providers
    live" note.
  - Production call sites:
    - `main.py` → `simplechart.bootstrap.bootstrap()` (discover, seal, then
      plugins) → `MainWindow(provider_registry)`;
    - `MainWindow._commit_provider_route` and `CapabilityRouter.route` for
      every data path;
    - `MainWindow._on_application_settings` → `ConnectionDialog`;
    - `BarComparisonService._source_requests`.
  - Integration tests: `test_discovered_provider_works_through_mainwindow_settings`
    (AC1), `test_main_seals_providers_before_loading_plugins`,
    `test_same_symbol_switch_discards_in_flight_details_earnings_and_name`,
    `test_main_window_data_quality_uses_registry_sources`.
  - Not verified: no manual run of the real app, and no live Alpaca or
    Yahoo call.
