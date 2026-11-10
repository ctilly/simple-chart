# Credential Security Contract

SimpleChart stores provider credentials only in the current user's operating-
system credential store through `data.provider.credentials.CredentialStore`.
This is a non-negotiable application security boundary.

## Permitted Storage

- Windows Credential Locker, macOS Keychain, or a supported Linux keyring.
- Test-only in-memory credential stores injected by automated tests. Test
  credentials must be synthetic and must never be constructed by production
  application code.

SQLite may store non-secret provider metadata such as connection ID,
environment, feed, display name, and active selection. It must never store an
API key, API secret, password, token, or serialized credential payload.

## Prohibited Storage And Fallbacks

Production code must never read or write provider credentials through:

- environment variables, including common provider variables such as
  `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`;
- SQLite or another application database;
- JSON, TOML, YAML, INI, `.env`, or other plaintext configuration files;
- command-line arguments;
- source files, generated files, logs, exception messages, telemetry, or
  clipboard persistence;
- an automatic in-memory fallback when the OS credential store is unavailable.

Missing packages, an unavailable keyring backend, denied access, or a failed
credential-store probe must disable every provider that requires credentials.
Yahoo remains available because it requires no credentials. The application
must explain the unavailable dependency and must not weaken storage security to
keep a provider enabled.

## Provider Implementation Rules

When adding or changing a credential-requiring provider:

1. Declare its package requirements and that it requires credentials in the
   provider registry.
2. Obtain credentials only through the injected `CredentialStore`.
3. Keep provider imports lazy so a missing optional provider cannot prevent
   Yahoo from starting.
4. Redact credential objects and never include credential values in errors or
   logs.
5. Disable configuration and activation until the secure-store preflight
   succeeds.
6. Add tests proving that missing dependencies produce no database, file, or
   environment-variable fallback.
7. Run the secure-store probe once during application startup and pass that
   result into every credential configuration and provider activation path.
   Do not rerun the probe whenever Settings opens.

The only acceptable response to unavailable secure storage is to leave the
credential-requiring provider unavailable.

## Error Output And Plugins

Only credentials are sensitive, but exception text from outside
SimpleChart (library and provider messages) can carry them. The guarantee:
an exception that reaches SimpleChart's UI, logging or process-wide output
channels is shown or written only as the safe facts below, never as its
text.

- **UI:** a fixed message for the failed action ("{action} failed. Details
  were logged."). Only a `UserFacingError`, whose text SimpleChart composes
  from app-controlled values, shows its own message. Providers translate
  their libraries' failures into such errors where they call the library.
- **Diagnostics:** exceptions are written only through
  `data.errors.describe`: type names along the cause chain, HTTP status,
  the app's context, and code locations, never exception text.
- **Pattern backstop:** `mask_patterns` masks key, secret, token, password
  and authorization values in text SimpleChart writes but did not compose.
  It is defense in depth, not the guarantee.
- **Process-wide channels:** `simplechart.output_safety.install()` runs first
  in both entry points, before any plugin is imported. It sanitizes every
  log record before any handler sees it, describing each exception that is
  its message or an argument, directly or inside lists, tuples, sets and
  mappings (past a nesting or size bound the argument is withheld).
  Third-party loggers, `warnings` and Qt messages are written as metadata
  only. It also installs the unraisable, thread and unhandled-exception
  hooks.
- **Known residual:** third-party code that formats exception text into a
  plain string itself and uses `stacklevel` to attribute the record to
  SimpleChart code is outside the guarantee; only the pattern backstop
  applies to that text.
- **Unhandled errors:** the process writes a `describe` summary and exits
  with status 1. The exception is never passed to a default hook.
- **Plugins:**
  - Plugins never receive credentials. `simplechart.api` exposes chart data
    and extension types only; no credential store, provider, or connection
    object is reachable through it.
  - Plugins are trusted code. They run in the application's process with
    its permissions; installing one is a decision to trust it.
  - If a plugin raises, while loading or in any extension or store-handler
    call, SimpleChart logs the failure through `describe` and turns the
    extension off until restart. A plugin that fails while loading is
    rolled back and skipped. The user sees only a fixed notice naming the
    extension or plugin file.
- **Regression check:** `tests/test_no_raw_exception_output.py` scans
  `main.py`, `app/`, `chart/`, `data/`, `simplechart/`, `tools/` and
  `indicators/` and fails on formatted exception text, exceptions
  (including any chained `__cause__` or `__context__`) passed to UI text or
  logging calls, `logger.exception`, `exc_info=`, and traceback
  formatting. Route new error output through `report_error`,
  `user_message` or `describe` instead of extending its allowlist.
