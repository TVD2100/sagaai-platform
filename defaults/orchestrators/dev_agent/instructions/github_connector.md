---
id: github_connector
name: GitHub Connector
description: How to use GitHub connection tools (list/create repos, read/upload/update files, optimized batch publishing). Load this instruction when the task involves GitHub repositories.
---

# GitHub Connector - Tool Usage Guide

You have access to GitHub through **enabled service connections**. Each
connection is identified by a `connector_id` listed in the `## Available
service connections` block of your system prompt. Tokens are handled by the
platform - never ask the user for a token, and never try to read or pass one.

---

## Two connector families

The service id of a connection determines which tool family to use:

- **`github`** (PyGithub connector) → `github_*` tools:
  `github_list_repos`, `github_create_repo`, `github_upload_file`,
  `github_update_file`, `github_read_file`.
- **`github_rest`** (direct REST API connector) → `ghr_*` tools:
  `ghr_list_repos`, `ghr_create_repo`, `ghr_read_file`, `ghr_upload_file`,
  `ghr_update_file`, `ghr_delete_file`, `ghr_list_files`, `ghr_batch_commit`,
  `ghr_batch_upsert`.

Use the tool family matching the connection's service id in the
`## Available service connections` block. When in doubt, the `github_*`
tools work with `github` connections only, and the `ghr_*` tools with
`github_rest` connections only.

---

## Tool signatures

Every tool returns a plain JSON dict:
- `{"ok": true, "result": ...}` on success;
- `{"ok": false, "error": "..."}` on failure (the error is user-facing; report
  it back to the user).

All `github_*` and `ghr_*` tools share the same argument conventions: the
first argument is always `connector_id`, `repo` accepts `"owner/repo"` or a
bare repo name of the authenticated user, and `message`/`branch`/`sha` are
optional where listed.

### `github_list_repos` / `ghr_list_repos`
Arguments:
- `connector_id` (str, required): connection id.
- `sort` (str, optional): `"updated"` (default) | `"created"` | `"full_name"`.
Returns a list of repos: `full_name`, `name`, `private`, `description`,
`html_url`, `default_branch`.

### `github_create_repo` / `ghr_create_repo`
Arguments:
- `connector_id` (str, required).
- `name` (str, required): repository name - **lowercase, no spaces**
  (GitHub rejects uppercase letters and spaces in new repo names).
- `description` (str, optional).
- `private` (bool, optional, default `true`): create a private repo.
Creates the repo with auto-init (README) under the authenticated user.
Returns `full_name`, `name`, `html_url`, `default_branch`.

### `github_upload_file` / `ghr_upload_file`
Create a **new** file in a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required): `"owner/repo"` or a bare repo name owned by the
  authenticated user.
- `path` (str, required): file path in the repo (e.g. `docs/guide.md`).
- `content` (str, required): full file content.
- `message` (str, optional): commit message (defaults to `Add <path>`).
- `branch` (str, optional): target branch (defaults to the repo default branch).
**Fails when the file already exists** - use the matching `*_update_file`
tool instead.

### `github_update_file` / `ghr_update_file`
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

### `github_read_file` / `ghr_read_file`
Read a text file from a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `path` (str, required).
- `branch` (str, optional): ref/branch to read from.
Returns `path`, `content` (UTF-8 text), `sha`, `url`.

### `ghr_delete_file` (REST only)
Delete a file from a repository.
Arguments: `connector_id`, `repo`, `path` (required); `message`, `branch`,
`sha` (optional; the SHA is fetched automatically when omitted).
Returns `path`, `committed`.

### `ghr_list_files` (REST only)
List the top-level entries of a repository directory.
Arguments: `connector_id`, `repo` (required); `path` (optional, `""` =
repository root), `branch` (optional).
Returns a list of `name`, `path`, `type` (`file` | `dir`).

### `ghr_batch_commit` (REST only) - publish many files in ONE commit
**Optimized for large file sets.** Uses the Git Data API: all blobs are
created first, then one tree, one commit and one ref update for the whole
batch. Prefer this over dozens of individual `upload_file` calls.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `files` (list | JSON string, required): `[{"path": "...", "content": "..."}]`.
- `message` (str, optional): commit message.
- `branch` (str, optional): target branch; **created automatically** when the
  repository has no commits yet.
Returns `commit_sha`, `tree_sha`, `total_files`, `files_created`, `committed`,
`ref_created`, `ref_updated`.

### `ghr_batch_upsert` (REST only) - batch with change detection
Like `ghr_batch_commit`, but compares each local file with the remote tree by
Git blob SHA and **skips unchanged files** (no commit is created when nothing
changed). Returns `files_created`, `files_updated`, `files_unchanged`,
`total_files`, `committed`, `commit_sha`.
Use this for incremental sync of a folder to a repository.

---

## Usage rules

1. **Always pass `connector_id` first.** Use exactly the id from
   `## Available service connections`, and use the tool family matching that
   connection's service (`github` → `github_*`, `github_rest` → `ghr_*`).
   When several connections are enabled, prefer the one matching the user's
   account/repo context; if unclear, ask the user which connection to use.
2. **Repo format:** prefer `"owner/repo"` when the repo is not obviously the
   user's own. A bare name resolves to the authenticated user's repo.
3. **New repo names:** lowercase only, no spaces. Private by default.
4. **Creating vs updating a file:** use the `*_upload_file` tool only for a
   **new** file; use the `*_update_file` tool for an **existing** file. When
   in doubt, read the repo/file listing first or attempt `upload_file` - if
   it errors with "File already exists", switch to `update_file`.
5. **Read before edit:** before updating a file, read it with the matching
   `*_read_file` tool so you can preserve and modify the existing content
   deliberately.
6. **Batch publishing:** for more than a handful of files use
   `ghr_batch_commit` (or `ghr_batch_upsert` for incremental syncs) - it
   creates ONE commit for the whole batch instead of one commit per file.
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
`*_list_repos` → pick a repo → `*_read_file` to inspect files.

### Create a repo and upload an initial file
1. `github_create_repo(name="my_project", description="...", private=true)`
   (or `ghr_create_repo` for a `github_rest` connection).
2. `github_upload_file(connector_id, repo="my_project", path="README.md", content="...")`.

### Update an existing file
1. `github_read_file(connector_id, repo, path)` → old content.
2. Modify content (e.g. apply a code change).
3. `github_update_file(connector_id, repo, path, content=new)`.

### Publish a whole project in one commit (REST)
1. Collect all project files as `[{"path": ..., "content": ...}, ...]`.
2. `ghr_batch_commit(connector_id, repo="owner/my_project", files=files,
   message="Initial publish")`.
3. To refresh the remote with only the changed files later, use
   `ghr_batch_upsert` with the same `files` list - unchanged files are
   skipped automatically.
