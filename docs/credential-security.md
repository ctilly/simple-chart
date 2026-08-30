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
