---
id: connector_creator
name: Connector Creator
description: How to create, fill and enable an external service connection (GitHub REST, SSH) on the user's request via the connector tools: draft without credentials, Connectors page flow, enabling for orchestrators and the per-connection memory file. Load when the user asks to create/configure a connector or connect a new server/account.
---

# Connector Creator - Guide for creating service connections via DevAgent

The user can ask you to create a connection to an external service (GitHub
account, SSH server, ...) right in the chat. You have the `list_*` /
`create_connection` / `update_connection` / `delete_connection` /
`test_connection` / `get_enabled_connections` / `set_enabled_connections` /
`get_connection_memory` / `set_connection_memory` tools for this. The created
connection appears on the **Connectors** page of the platform, where its
credentials are stored encrypted at rest.

---

## Golden rule: no credentials in the chat

Tokens, passwords and private keys must never travel through the chat when it
can be avoided: chat history keeps every message, and copying a secret into
the dialogue is the leak that this flow is designed to prevent.

- **Default flow:** create a **draft** connection (`pending=true`) with only
the non-secret data (service, name, SSH host/port/username). The draft
immediately appears on the Connectors page with the "credentials required"
status; the USER fills the token/password/key there through the password
fields. Nothing secret is ever typed into the chat.
- **If the user insists on passing a secret in the chat:** you may accept it
and pass it directly to `create_connection` / `update_connection` (the
platform encrypts it at rest). Then:
  1. never repeat the secret back, never write it into any file, journal,
     report or the connection's `memory.md`;
  2. tell the user the value was stored encrypted and is hidden.

---

## Step-by-step workflow

1. **Discover the available services.** Call `list_connector_services` and
   pick the service the user needs (`github_rest` for GitHub REST API, `ssh`
   for SSH/SFTP servers). Unknown service -> say which services exist and ask
   the user.
2. **Collect the non-secret data.** Ask the user only for:
   - GitHub REST: a display name (and optionally the account/login);
   - SSH: a display name, host, port (default 22), username. When the server
     is new to you, ALSO follow the "first connection to an unknown server"
     rules from the `ssh_connector` instruction: find out what kind of server
     it is (hosting provider / VPS / cloud / on-premise), whether the provider
     documents restrictions (ports, daemons, panels, access mode), and whether
     there is existing documentation.
   Do NOT ask for tokens/passwords/keys in the chat - they go to the
   Connectors page (see the golden rule).
3. **Create the draft.** `create_connection(service, name, config=...,
   pending=true)`. Report the connection id and that the connection is now
   visible on the Connectors page with the "credentials required" status.
4. **Ask the user to fill the credentials** on the Connectors page (the
   platform encrypts them), then confirm readiness (e.g. "done").
5. **Verify.** `test_connection(conn_id)` - a clean ok/error result. A
   pending draft returns a clear "credentials have not been set yet" error:
   that means the user has not filled the credentials yet. On a real failure,
   report it and suggest a concrete fix. For SSH remember: do not run
   destructive commands during the test.
6. **Enable for orchestrators.** When the user wants you (or another
   orchestrator) to work with this connection, call
   `set_enabled_connections([<id>, ...], orchestrator=<slug>)` (omitting
   `orchestrator` enables it for yourself). Use `get_enabled_connections` to
   show the current list. The connection tools appear only for enabled
   connections, so do not skip this step.
7. **Seed the memory file.** Immediately after a successful test or the
   first real use, save the facts already learned into the connection's
   memory: `set_connection_memory(conn_id, content)` - server/provider kind,
   limits, important paths, access mode, gotchas. No secrets, ever. When the
   user gives you facts (paths, restrictions, documentation links), store the
   important ones there.

---

## Per-connection memory file (mandatory discipline)

Every connection keeps an agent-maintained notes file (`memory.md` in its
folder), managed ONLY through `get_connection_memory` /
`set_connection_memory`.

- **Before working** with a connection: read its memory
  (`get_connection_memory`) AND load the matching connector instruction
  (`ssh_connector` / `github_connector`) if it is listed in
  `## Available instructions`.
- **After finishing a task** that used the connection: when you discovered
  something worth keeping (limits, paths, rules, gotchas, the server type),
  OFFER to update the memory and do it after the user agrees. Keep it short
  and factual.
- **Never store secrets** in the memory file: no tokens, passwords, keys,
  passphrases or personal data of the user.

---

## Safety & hygiene

- Never ask the user to paste credentials into the chat; direct them to the
  Connectors page (password fields).
- Never echo a secret you received, and never persist it outside the
  encrypted manifest.
- `delete_connection` removes the connection permanently; ask for an explicit
  confirmation before calling it.
- The connection id is a short technical id (printed as 8 hex chars); the
  display name is what the user sees - choose a clear, unique name.
- Keep every tool result free of secrets: the public manifests returned by
  the tools contain no credential material.
