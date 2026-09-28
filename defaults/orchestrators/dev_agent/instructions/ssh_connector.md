---
id: ssh_connector
name: SSH Connector
description: How to use the SSH connection tools (ssh_*): run shell commands on a remote server, list/read/write/upload files over SFTP, test the connection. Load this instruction when the task involves remote servers over SSH.
---

# SSH Connector - Tool Usage Guide

You have access to remote servers through **enabled service connections** of
type `ssh`. Each connection is identified by a `connector_id` listed in the
`## Available service connections` block of your system prompt. Credentials
(password or private key) are handled by the platform - never ask the user
for them, and never try to read or pass them.

All tools of this connector are named **`ssh_*`** and work with any `ssh`
connection.

---

## Tool signatures

Every tool returns a plain JSON dict:
- `{"ok": true, "result": ...}` on success;
- `{"ok": false, "error": "..."}` on failure (the error is user-facing;
  report it back to the user).

The first argument is always `connector_id`.

### `ssh_test_connection`
Validate a connection and refresh its account info (shown on the connectors
page as `user@host`).
Arguments: `connector_id` (required).
Returns `ok`, `host`, `port`, `username`.

### `ssh_exec`
Run one shell command on the remote server.
Arguments:
- `connector_id` (str, required).
- `command` (str, required): the shell command to execute.
- `timeout` (number | str, optional): seconds to wait; default 60,
  maximum 300.
Returns `command`, `exit_status`, `stdout`, `stderr`, `stdout_truncated`,
`stderr_truncated`, `timeout_seconds`. `stdout`/`stderr` are UTF-8 text,
each truncated to 100 KB (the truncation flags tell you when). A non-zero
`exit_status` is still `ok: true` - check it explicitly.

### `ssh_list_dir`
List a remote directory over SFTP.
Arguments:
- `connector_id` (str, required).
- `path` (str, optional, default `"."`): remote directory.
Returns a list of `name`, `path`, `type` (`dir` | `file` | `link`), `size`,
`mtime`, sorted by name.

### `ssh_read_file`
Read a UTF-8 text file from the remote server.
Arguments: `connector_id`, `path` (required).
Returns `path`, `content`, `size`, `truncated`. The content is capped at
256 KB (`truncated: true` when the file is larger); non-UTF-8 (binary)
files are rejected with an error - use `ssh_exec` (e.g. `file`, `base64`
via the shell) to inspect them.

### `ssh_write_file`
Write a UTF-8 text file to the remote server, **overwriting** the remote
file content.
Arguments:
- `connector_id` (str, required).
- `path` (str, required): remote file path.
- `content` (str, required): full file content (max 1 MB).
- `create_dirs` (bool, optional): create the remote parent chain
  (mkdir -p style) before writing.
Returns `path`, `size`, `written`. Prefer `ssh_upload_file` for local
files; use `ssh_write_file` only for small inline content.

### `ssh_upload_file`
Upload a LOCAL file from the workspace to the remote server over SFTP
(streamed in chunks; capped at 50 MB). This is the safe way to publish
files: content does not travel through tool arguments and the transfer is
checked with sha256.
Arguments:
- `connector_id` (str, required).
- `local_path` (str, required): workspace-relative path of the local file
  (or an absolute path inside the workspace root).
- `remote_path` (str, required): remote file path (overwritten).
- `base_dir` (str, optional): workspace root to read from (defaults to
  the active DevAgent workspace).
- `create_dirs` (bool, optional): create the remote parent chain
  (mkdir -p style) before writing.
- `verify` (bool, optional, default `true`): re-read the remote file and
  compare sha256 digests.
Returns `path`, `size`, `sha256_local`, `verified`, `sha256_remote`,
`create_dirs`. Only report success when `verified` is `true`.

---

## Usage rules

1. **Always pass `connector_id` first.** Use exactly the id from
   `## Available service connections`. When several connections are enabled,
   prefer the one matching the user's host/server context; if unclear, ask
   the user which connection to use.
2. **Start with `ssh_test_connection`** when unsure whether the connection
   works: it reports a clean, credential-free error when the host, port or
   key is wrong.
3. **Read before overwrite.** Before `ssh_write_file`, read the current
   content with `ssh_read_file` so you can preserve and modify it
   deliberately.
4. **Verify after writing.** Re-read the file with `ssh_read_file` (or check
   it with `ssh_exec`, e.g. `wc -c`, `ls -l`) and report the verification
   result to the user together with the change.
5. **Destructive commands need care.** `ssh_exec` runs with the connection
   user's privileges; before `rm`/`mv`/`systemctl`-style commands, explain
   the intended effect to the user and prefer the least destructive variant.
6. **Check `exit_status`.** A failed command usually returns a non-zero exit
   status - surface its `stderr` to the user instead of treating the call as
   a success.
7. **Errors:** when a tool returns `{"ok": false, ...}`, explain the issue to
   the user and suggest a concrete next action (check host/port, key
   permissions, network, or the connection on the Connectors page).
8. **Never ask for or expose credentials.** If authentication fails, tell the
   user to check the connection on the Connectors page.
9. **Publish local files with `ssh_upload_file`.** Do not try `scp`/`rsync`
   from the machine running DevAgent: platform credentials live only inside
   the connector. Stream files with `ssh_upload_file` (sha256-verified)
   instead of passing content through tool arguments; keep `ssh_write_file`
   for small inline edits only.

---

## Common workflows

### Inspect a server
`ssh_test_connection` -> `ssh_exec(command="uname -a && uptime")` ->
`ssh_list_dir(path="/var/log")`.

### Read a config file
`ssh_list_dir(path="/etc/nginx")` -> `ssh_read_file(path="/etc/nginx/nginx.conf")`.

### Update a config file
1. `ssh_read_file(connector_id, "/etc/app/config.yml")` -> old content.
2. Modify the content.
3. `ssh_write_file(connector_id, "/etc/app/config.yml", content=new)`.
4. Verify with `ssh_read_file` or `ssh_exec(command="cat /etc/app/config.yml")`.

### Check a service
`ssh_exec(command="systemctl status nginx --no-pager")` - read the
`exit_status` and `stdout`.

### Publish local files to a server
1. Confirm the destination with `ssh_list_dir(path="<target dir>")`.
2. Back up files you are about to overwrite:
   `ssh_exec(command="cp -a <file> <file>.bak")`.
3. Upload each changed file with `ssh_upload_file` (pass `create_dirs=true`
   for new directories); require `"verified": true` in every result and
   re-upload when it is false.
4. Cross-check on the server when needed:
   `ssh_exec(command="sha256sum <remote file>")` - the digest must equal
   `sha256_local`.
5. Clean up backups and temp files after the user confirms the update.
