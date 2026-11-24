# Data Provider Skill

Use this when adding or changing a project data source. Read this file in full
before writing code.

## Step 1 — Read the contracts and exemplar

- [Credential Security Contract](../credential-security.md) — mandatory
  storage, dependency, and error-output rules.
- [Provider declaration](../../data/provider/spec.py),
  [provider interface](../../data/provider/base.py), and
  [discovery and validation](../../data/provider/registry.py) — supported
  declarations, methods, and validation rules.
- [Alpaca declaration](../../providers/alpaca/__init__.py) and
  [Alpaca implementation](../../providers/alpaca/implementation.py) — the
  exemplar for a credentialed provider with an optional SDK, connection
  templates, and feed-specific cache namespaces.

## Step 2 — Specify the provider

Produce a short spec and wait for user approval before coding. Include the
provider name and label, supported symbols and capabilities, native
timeframes and history limits, connection IDs, namespaces, non-secret option
choices and defaults, credential field names, optional dependencies, the
activation check, Yahoo fallback behavior, and the offline test plan. Get
explicit approval for any new dependency.

## Step 3 — Declare the package

Create `providers/<name>/` and export one module-level
`PROVIDER: ProviderSpec` in `__init__.py`. Keep the declaration, factory, and
any symbol eligibility predicate there. Put the `DataProvider` subclass and
SDK integration in `implementation.py`, which the factory imports lazily.
List optional dependencies as `(import_name, distribution_name)` pairs in
`required_modules`. A missing SDK must leave the app and Yahoo available.

- List only implemented capabilities in the `frozenset[Capability]`.
  Override every declared capability's method and `native_timeframes()` if
  you provide bars. Implement a provider-owned `check_connection()` that
  exercises the actual connection and works within the declared capabilities.
- Give every `ConnectionTemplate` a stable ID equal to the provider name
  or starting with `<name>-`. The `availability-probe-` prefix is reserved.
- Declare each `NamespaceSpec` with the provider name or `<name>:<suffix>`,
  a label, and the option values that select it. Every valid connection option
  combination must resolve to exactly one namespace. Use empty selectors for
  a single namespace and set `comparable` for sources suitable for Compare Bars.
- Declare non-secret settings as `OptionField` entries with distinct
  `(value, label)` choices and a default from those choices. Use
  `user_editable` and template `fixed_options` to control which settings
  appear in the connection dialog. SQLite persists these choices.
- Declare each credential as a `CredentialField` with a label and a
  unique key matching `[a-z][a-z0-9_]*`. Settings generates a masked input
  for each field. The factory receives the connection's validated
  `ProviderCredentials | None` from the registry. Read values with
  `credentials.get(key)` and pass them directly to the SDK. Production
  storage goes through `CredentialStore` to the OS keyring. Keep credentials
  out of configuration, environment variables, SQLite, representations,
  and error or log output.

Discovery validates the declaration and seals the registry before extension
plugins load. The app builds Settings and routes from these declarations.
No controller wiring is needed. Project provider packages and user plugin
files execute in the application process with its permissions. Any module
in that process can import `keyring` directly. Sealing and declarative
registration protect supported application paths from accidental or casual
misuse. Treat code in the project tree and `~/.simplechart/plugins` as
trusted in-process code. The plugin folder is only for chart extensions.

## Step 4 — Implement and route data

Return the models from `data/models.py`. Return bars oldest-first with
UTC-aware timestamps. Return news timestamps normalized to aware UTC, or
`None` for missing timestamps. Keep symbol and timeframe context in method
arguments, and let the aggregator synthesize non-native timeframes.

Translate SDK failures using the error types in `data/provider/base.py`.
Pass the application-controlled arguments those types expect. When an error
type accepts a message, pass a string literal or an f-string whose only
interpolations are a `display_name` name or attribute. Provider packages
must not subclass `UserFacingError` or display or log exception text.
Unexpected failures receive fixed UI messages and metadata-only diagnostics
through the app.

Yahoo supplies undeclared capabilities and requests for symbols excluded by
`serves_symbol`. For example, a bars-and-profile provider gets quotes,
snapshots, company names, news, search, earnings, and dividends from Yahoo.
An empty result from a declared capability stays empty.

## Step 5 — Verify

In the project's dev environment, run the discovered-provider conformance
checks and the application integration example.

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider tests/test_data_provider_boundaries.py
QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider tests/test_data_provider_integration.py
```

Model a provider-specific integration test on
`test_discovered_provider_works_through_mainwindow_settings`. Use the
production discovery function, the real MainWindow and Settings, an injected
in-memory keyring backend with synthetic credentials, and offline SDK
fixtures. For a provider declaring NEWS, add a raw response fixture to the
conformance test so it runs your real news parser.

- [ ] Discovery accepts the declaration, and every capability and the
  connection check have an implementation.
- [ ] Settings shows the expected connection, choice controls, and masked
  credentials. Saving and activating work through the production path.
- [ ] Data and source labels come from the expected provider, including
  Yahoo fallback. News timestamps satisfy the UTC contract.
- [ ] Credential values stay out of SQLite and its sidecars, logs,
  representations, and error messages.
- [ ] Conformance and integration tests run offline. Ruff and mypy pass.
