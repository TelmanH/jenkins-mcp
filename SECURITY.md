# Security

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead:
repository -> **Security** tab -> **Report a vulnerability**. You will get an answer within a few days.

## Threat model

The server gives an AI assistant read access to Jenkins. It is designed so that neither a mistake of the
assistant nor text inside a build log can change Jenkins or leak credentials.

| Risk | Protection |
|---|---|
| The assistant (or a prompt injection in a log) tries to trigger, stop, delete or configure something | The HTTP session refuses every method except GET/HEAD before a request is sent (`ReadOnlySession`, `src/jenkins_mcp/client.py`). There is no write tool for Jenkins. |
| Credentials sent to a foreign host (e.g. a URL pasted into the chat) | Requests only go to the configured instance URLs; other hosts are refused before any request. |
| Secrets in tool output (log lines, parameters, env.log, artifacts) | Values of secret-looking names and inline patterns (`password=`, `token:`, `Authorization:` headers, `user:pw@` in URLs, XML elements such as `<password>`, Jenkins-encrypted `{AQAA...}`
values) are masked. Configuration tools show credential **IDs** only. **Best effort**: unusual secret formats can pass; rely on Jenkins' own credential masking first. |
| Prompt injection in logs | Every log excerpt is labelled as untrusted data for the assistant; the server cannot act on it anyway (read-only). |
| Token leak from the config | On Windows, `setup.ps1` makes VS Code ask for each token once and keep it in its secure storage (`.vscode/mcp.json` `inputs`, `password: true`), so no token file is needed. Otherwise tokens live in `config/.env` (git-ignored) or environment variables, never in the YAML. Personal tokens carry your own permissions; the server still sends GET/HEAD only, and `list_instances(check_connection)` shows whether a token has admin rights. |
| Local files | The only writes: new drafts in `knowledge/drafts/` by `draft_kb_entry` / `propose_kb_update` (never overwrites) and the log cache (`~/.cache/jenkins-mcp`, owner-only folder). The cache holds **raw** console logs - delete it if that matters on a shared machine. On Windows the folder permissions are not changed; it is protected by the ACL of your user profile. |
| TLS | Certificates are verified by default. `verify_tls: false` exists for broken internal setups; prefer `ca_bundle`. |

Out of scope: the security of the MCP client itself (VS Code Copilot, Claude Code), of Jenkins, and of
the machine the server runs on. Regular expressions passed to `search_log` run locally on the downloaded log;
a pathological pattern can make one call slow.

## Supported versions

Only the latest release receives fixes.
