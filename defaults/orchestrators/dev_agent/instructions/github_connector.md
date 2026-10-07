---
id: github_connector
name: GitHub Connector
description: How to use the GitHub REST connection tools (ghr_*): list/create repos, read/upload/update/delete files, metadata and Git object queries, optimized batch publishing. Load this instruction when the task involves GitHub repositories.
---

# GitHub Connector - Tool Usage Guide (REST API)

You have access to GitHub through **enabled service connections**. Each
connection is identified by a `connector_id` listed in the `## Available
service connections` block of your system prompt. Tokens are handled by the
platform - never ask the user for a token, and never try to read or pass one.

There is ONE connector family: **`github_rest`** (direct REST API v3,
`requests`, no PyGithub). All its tools are named **`ghr_*`** and work with
any `github_rest` connection. The legacy `github` (PyGithub) connector and
its `github_*` tools no longer exist - do not call them.

**REST-only rule: never `git push`.** All publishing to GitHub goes
through these `ghr_*` tools exclusively. Do not use shell commands
(`git push`, `gh`, `curl`) to publish, and do not guide the user to
publish from inside an assistant task that way: tokens stay inside the
platform and every change must be verifiable through the tools below.
Large local files are published via `ghr_batch_commit_paths` (which reads
files from the workspace disk), never via shell.

---

## Pre-publish secret & personal-data check (MANDATORY)

Before ANY publishing call - `ghr_upload_file`, `ghr_update_file`,
`ghr_delete_file`, `ghr_batch_commit`, `ghr_batch_commit_paths`,
`ghr_batch_upsert` - scan every outgoing file for secrets AND personal
data. This step is mandatory and is NOT waived by the user's approval to
publish: an approved batch can still leak a key or expose someone's
personal data.

What to scan for (the actual content of every file in the outgoing payload):
- API keys and access tokens (OpenAI / Yandex / GigaChat / DeepSeek keys,
  GitHub PATs `ghp_...` / `github_pat_...`, AWS `AKIA...`, Telegram bot
  tokens, and similar);
- OAuth client secrets, service-account JSON, connection strings with
  embedded credentials;
- passwords / passphrases, values of `SAGAAI_AUTH_PASSWORD` and similar
  environment variables;
- private keys and certificates: `-----BEGIN ... PRIVATE KEY-----` blocks,
  `.pem`, `.key`, `.p12`, `.pfx`;
- dotenv-style files (`.env`, `.env.*`, `secrets.*`, `credentials.*`) and
  any file whose name signals secrets;
- high-entropy literals (long base64 / hex strings) assigned to
  `token` / `secret` / `api_key` / `password` / `client_secret`;
- platform connection tokens - never reproduce or re-embed them anywhere.

Personal data (PII) - scan for anything that identifies a real person:
- names together with contact details (email, phone, messenger handle,
  postal address);
- government / financial identifiers (passport, SSN, tax or national ID
  numbers, bank account or card numbers);
- financial, medical or biometric records and any special-category data;
- user account dumps and exports (databases, CSV/JSON exports), chat or
  access logs that contain names, emails, IPs or user ids;
- customer / employee lists and any contact database.

How to scan: build the explicit list of outgoing paths, then check their
ACTUAL content (not only the file names) against the patterns above before
calling the publish tool. For `ghr_batch_commit_paths` the files come from
disk - read and scan that same disk content; do not trust the path list
alone.

On a SECRET hit:
1. STOP - do not publish that file.
2. Drop it from the payload; if it is a real secret, add it to `.gitignore`
   and replace the literal with an environment variable.
3. Report to the user with the value MASKED (file + line + secret kind,
   never the secret itself) and ask how to proceed.

On a PERSONAL-DATA hit:
1. STOP - do not publish that file.
2. Drop it from the payload.
3. Report to the user WHAT was found, with the value MASKED (file + line +
   kind of personal data, never the value itself), and ASK whether the file
   or the data must be removed (or anonymised).
4. Never delete, rewrite or anonymise the user's data on your own - act only
   on an explicit answer. Publish the file again only after the user
   explicitly confirms it is safe to share.

Rule: never publish a secret or personal data even to a PRIVATE repository -
Git history keeps it forever. A leaked key must be treated as compromised and
rotated; exposed personal data cannot be un-published.

---

## Tool signatures

Every tool returns a plain JSON dict:
- `{"ok": true, "result": ...}` on success;
- `{"ok": false, "error": "..."}` on failure (the error is user-facing; report
  it back to the user).

All `ghr_*` tools share the same argument conventions: the first argument is
always `connector_id`; `repo` accepts `"owner/repo"` or a bare repo name of
the authenticated user; `message`/`branch`/`sha` are optional where listed.

### `ghr_list_repos`
Arguments:
- `connector_id` (str, required): connection id.
- `sort` (str, optional): `"updated"` (default) | `"created"` | `"full_name"`.
Returns a list of repos: `full_name`, `name`, `private`, `description`,
`html_url`, `default_branch`.

### `ghr_create_repo`
Arguments:
- `connector_id` (str, required).
- `name` (str, required): repository name - **lowercase, no spaces**
  (GitHub rejects uppercase letters and spaces in new repo names).
- `description` (str, optional).
- `private` (bool, optional, default `true`): create a private repo.
Creates the repo with auto-init (README) under the authenticated user.
Returns `full_name`, `name`, `html_url`, `default_branch`.

### `ghr_read_file`
Read a text file from a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `path` (str, required).
- `branch` (str, optional): ref/branch to read from.
Returns `path`, `content` (UTF-8 text), `sha`, `url`.

### `ghr_upload_file`
Create a **new** file in a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required): `"owner/repo"` or a bare repo name owned by the
  authenticated user.
- `path` (str, required): file path in the repo (e.g. `docs/guide.md`).
- `content` (str, required): full file content.
- `message` (str, optional): commit message (defaults to `Add <path>`).
- `branch` (str, optional): target branch (defaults to the repo default branch).
**Fails when the file already exists** - use `ghr_update_file` instead.

### `ghr_update_file`
Update an **existing** file in a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required): same format as above.
- `path` (str, required).
- `content` (str, required): new file content.
- `message` (str, optional): commit message (defaults to `Update <path>`).
- `branch` (str, optional).
- `sha` (str, optional): expected current file SHA; **fetched automatically**
  when omitted - normally you do not need to pass it.

### `ghr_delete_file`
Delete a file from a repository.
Arguments: `connector_id`, `repo`, `path` (required); `message`, `branch`,
`sha` (optional; the SHA is fetched automatically when omitted).
Returns `path`, `committed`.

### `ghr_list_files`
List the top-level entries of a repository directory.
Arguments: `connector_id`, `repo` (required); `path` (optional, `""` =
repository root), `branch` (optional).
Returns a list of `name`, `path`, `type` (`file` | `dir`).

### `ghr_test_connection`
Validate a connection and refresh its account info.
Arguments: `connector_id` (required).
Returns `ok`, `login`, `name`, `id`, `html_url`.

### `ghr_get_repo_info`
Return metadata for one repository.
Arguments: `connector_id`, `repo` (required).
Returns `full_name`, `name`, `owner`, `private`, `default_branch`, ...

### `ghr_read_file_meta`
Return file metadata **without content**.
Arguments: `connector_id`, `repo`, `path` (required); `branch` (optional).
Returns `path`, `sha`, `size`, `url`.

### `ghr_get_ref`
Return a branch head ref.
Arguments: `connector_id`, `repo` (required); `branch` (optional, default
branch when empty), `resolve` (bool, optional, default `true`).
Returns `ref`, `sha`, `object_type`.

### `ghr_get_commit`
Return a Git commit object.
Arguments: `connector_id`, `repo`, `commit_sha` (required).
Returns `sha`, `message`, `tree_sha`, `parents`.

### `ghr_get_tree`
Return the repository tree for a branch.
Arguments: `connector_id`, `repo` (required); `branch` (optional),
`recursive` (bool, optional, default `false`).
Returns `tree_sha`, `truncated`, `entries`.

### `ghr_batch_commit` - publish many files in ONE commit (PREFERRED)
**The primary way to publish multiple files.** Uses the Git Data API: all
blobs are created first, then one tree, one commit and one ref update for
the whole batch.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `files` (list | JSON string, required): `[{"path": "...", "content": "..."}]`.
- `message` (str, optional): commit message.
- `branch` (str, optional): target branch; **created automatically** when the
  repository has no commits yet.
Returns `commit_sha`, `tree_sha`, `total_files`, `files_created`,
`files_updated`, `files_unchanged`, `committed`, `ref_created`,
`ref_updated`. The counters are **honest**: each path is compared against
the remote tree BEFORE publishing - a brand-new path counts as
`files_created`, a different blob as `files_updated`, an identical blob as
`files_unchanged`. Every listed file still lands in the commit; the
counters only report what actually changed. Fast-forward conflicts on the
ref update are retried automatically once (the result carries
`ref_retried`).

### `ghr_batch_commit_paths` - publish local files from disk (for LARGE files)
Reads files from the workspace disk and publishes them in ONE commit via
the same Git Data API pipeline as `ghr_batch_commit`. **Preferred for
large files** (or large batches) whose content cannot be inlined into a
tool call.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `paths` (list[str], required): relative repo paths inside `base_dir`
  (e.g. `["README.md", "docs/guide.md"]`). Absolute paths and paths that
  escape `base_dir` are rejected.
- `message` (str, optional): commit message.
- `branch` (str, optional): target branch (created when missing).
- `base_dir` (str, optional): the local root to read from (defaults to the
  active DevAgent workspace root; pass it explicitly when publishing from
  another directory).
Files must be UTF-8 text. Returns the same summary as `ghr_batch_commit`.

### `ghr_batch_upsert` - batch with change detection
Like `ghr_batch_commit`, but compares each local file with the remote tree by
Git blob SHA and **skips unchanged files** (no commit is created when nothing
changed). Returns `files_created`, `files_updated`, `files_unchanged`,
`total_files`, `committed`, `commit_sha`.
Use this for incremental sync of a folder to a repository.

---

## Usage rules

1. **Always pass `connector_id` first.** Use exactly the id from
   `## Available service connections`. When several connections are enabled,
   prefer the one matching the user's account/repo context; if unclear, ask
   the user which connection to use.
2. **Batch first.** For TWO or more files always use `ghr_batch_commit`
   (or `ghr_batch_upsert` for an incremental sync) - it creates ONE commit
   for the whole batch instead of one commit per file. The single-file tools
   (`ghr_upload_file` / `ghr_update_file` / `ghr_delete_file`) are for
   one-file operations only.
3. **Repo format:** prefer `"owner/repo"` when the repo is not obviously the
   user's own. A bare name resolves to the authenticated user's repo.
4. **New repo names:** lowercase only, no spaces. Private by default.
5. **Creating vs updating a file:** use `ghr_upload_file` only for a **new**
   file; use `ghr_update_file` for an **existing** file. When in doubt, read
   the repo/file listing first or attempt `ghr_upload_file` - if it errors
   with "File already exists", switch to `ghr_update_file`.
6. **Read before edit:** before updating a file, read it with `ghr_read_file`
   so you can preserve and modify the existing content deliberately.
7. **Verify after writing.** Do not trust the success summary alone - when
   a task publishes or changes files, verify at least one operation:
   - re-read a changed file with `ghr_read_file` and compare its content/SHA;
   - for batch operations, check the `commit_sha` with `ghr_get_commit`,
     or scan the tree with `ghr_get_tree(repo, branch, recursive=true)`
     and confirm the expected paths are present.
   Report the verification result to the user together with the change.
8. **Errors:** when a tool returns `{"ok": false, ...}`, explain the issue
   to the user and suggest a concrete next action (e.g. verify token
   permissions, use a different repo name, or update instead of upload).
9. **Never ask for or expose tokens.** If authentication fails, tell the user
   to check the connection on the Connectors page.
10. **Secret & personal-data check is mandatory before publishing.**
    Before any publishing call (`ghr_upload_file` / `ghr_update_file` /
    `ghr_delete_file` / `ghr_batch_commit` / `ghr_batch_commit_paths` /
    `ghr_batch_upsert`), scan every outgoing file's content for secrets and
    personal data per the "Pre-publish secret & personal-data check
    (MANDATORY)" section. Never publish a file that contains a key, token,
    password, private key or personal data - the user's approval to publish
    does not override this rule.

---

## Common workflows

### Inspect repositories
`ghr_list_repos` → pick a repo → `ghr_list_files` / `ghr_read_file` to
inspect files.

### Create a repo and publish files
1. `ghr_create_repo(name="my_project", description="...", private=true)`.
2. Run the pre-publish secret & personal-data check on every file you are
   about to send.
3. Collect the files and publish them in ONE commit:
   `ghr_batch_commit(connector_id, repo="my_project",
   files=[{"path": "README.md", "content": ...}, ...], message="Initial publish")`.

### Update an existing file
1. `ghr_read_file(connector_id, repo, path)` → old content.
2. Modify content (e.g. apply a code change).
3. `ghr_update_file(connector_id, repo, path, content=new)`.

### Publish a whole project in one commit (preferred for many files)
1. Collect all project files as `[{"path": ..., "content": ...}, ...]`.
2. Run the pre-publish secret & personal-data check on every file; drop
   any file with a secret or personal data from the batch and report it
   (masked) to the user.
3. `ghr_batch_commit(connector_id, repo="owner/my_project", files=files,
   message="Initial publish")`.
4. To refresh the remote with only the changed files later, use
   `ghr_batch_upsert` with the same `files` list - unchanged files are
   skipped automatically.
