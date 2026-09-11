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
Returns `commit_sha`, `tree_sha`, `total_files`, `files_created`, `committed`,
`ref_created`, `ref_updated`. Fast-forward conflicts on the ref update are
retried automatically once (the result carries `ref_retried`).

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
7. **After a write**, report the result: file path(s), commit SHA if present,
   and confirmation that the change was committed.
8. **Errors:** when a tool returns `{"ok": false, ...}`, explain the issue
   to the user and suggest a concrete next action (e.g. verify token
   permissions, use a different repo name, or update instead of upload).
9. **Never ask for or expose tokens.** If authentication fails, tell the user
   to check the connection on the Connectors page.

---

## Common workflows

### Inspect repositories
`ghr_list_repos` → pick a repo → `ghr_list_files` / `ghr_read_file` to
inspect files.

### Create a repo and publish files
1. `ghr_create_repo(name="my_project", description="...", private=true)`.
2. Collect the files and publish them in ONE commit:
   `ghr_batch_commit(connector_id, repo="my_project",
   files=[{"path": "README.md", "content": ...}, ...], message="Initial publish")`.

### Update an existing file
1. `ghr_read_file(connector_id, repo, path)` → old content.
2. Modify content (e.g. apply a code change).
3. `ghr_update_file(connector_id, repo, path, content=new)`.

### Publish a whole project in one commit (preferred for many files)
1. Collect all project files as `[{"path": ..., "content": ...}, ...]`.
2. `ghr_batch_commit(connector_id, repo="owner/my_project", files=files,
   message="Initial publish")`.
3. To refresh the remote with only the changed files later, use
   `ghr_batch_upsert` with the same `files` list - unchanged files are
   skipped automatically.
