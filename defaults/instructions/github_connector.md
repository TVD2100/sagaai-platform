---
id: github_connector
name: GitHub Connector
description: How to use GitHub connection tools (list/create repos, read/upload/update files). Load this instruction when the task involves GitHub repositories. Available globally to every orchestrator that has a GitHub service connection enabled.
---

# GitHub Connector - Tool Usage Guide

You have access to GitHub through **enabled service connections**. Each
connection is identified by a `connector_id` listed in the `## Available
service connections` block of your system prompt. Tokens are handled by the
platform - never ask the user for a token, and never try to read or pass one.

---

## Tool signatures

Every tool returns a plain JSON dict:
- `{"ok": true, "result": ...}` on success;
- `{"ok": false, "error": "..."}` on failure (the error is user-facing; report
  it back to the user).

### `github_list_repos`
Arguments:
- `connector_id` (str, required): connection id.
- `sort` (str, optional): `"updated"` (default) | `"created"` | `"full_name"`.
Returns a list of repos: `full_name`, `name`, `private`, `description`,
`html_url`, `default_branch`.

### `github_create_repo`
Arguments:
- `connector_id` (str, required).
- `name` (str, required): repository name - **lowercase, no spaces**
  (GitHub rejects uppercase letters and spaces in new repo names).
- `description` (str, optional).
- `private` (bool, optional, default `true`): create a private repo.
Creates the repo with auto-init (README) under the authenticated user.
Returns `full_name`, `name`, `html_url`, `default_branch`.

### `github_upload_file`
Create a **new** file in a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required): `"owner/repo"` or a bare repo name owned by the
  authenticated user.
- `path` (str, required): file path in the repo (e.g. `docs/guide.md`).
- `content` (str, required): full file content.
- `message` (str, optional): commit message (defaults to `Add <path>`).
- `branch` (str, optional): target branch (defaults to the repo default branch).
**Fails when the file already exists** - use `github_update_file` instead.

### `github_update_file`
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

### `github_read_file`
Read a text file from a repository.
Arguments:
- `connector_id` (str, required).
- `repo` (str, required).
- `path` (str, required).
- `branch` (str, optional): ref/branch to read from.
Returns `path`, `content` (UTF-8 text), `sha`, `url`.

---

## Usage rules

1. **Always pass `connector_id` first.** Use exactly the id from
   `## Available service connections`. When several connections are enabled,
   prefer the one matching the user's account/repo context; if unclear, ask
   the user which connection to use.
2. **Repo format:** prefer `"owner/repo"` when the repo is not obviously the
   user's own. A bare name resolves to the authenticated user's repo.
3. **New repo names:** lowercase only, no spaces. Private by default.
4. **Creating vs updating a file:** use `github_upload_file` only for a
   **new** file; use `github_update_file` for an **existing** file. When in
   doubt, read the repo/file listing first or attempt `upload_file` - if it
   errors with "File already exists", switch to `update_file`.
5. **Read before edit:** before updating a file, read it with
   `github_read_file` so you can preserve and modify the existing content
   deliberately.
6. **After a write**, report the result: file path, commit SHA if present,
   and confirmation that the change was committed.
7. **Errors:** when a tool returns `{"ok": false, ...}`, explain the issue
   to the user and suggest a concrete next action (e.g. verify token
   permissions, use a different repo name, or update instead of upload).
8. **Never ask for or expose tokens.** If authentication fails, tell the user
   to check the connection on the Connectors page.

---

## Common workflows

### Inspect repositories
`github_list_repos` → pick a repo → `github_read_file` to inspect files.

### Create a repo and upload an initial file
1. `github_create_repo(name="my_project", description="...", private=true)`.
2. `github_upload_file(connector_id, repo="my_project", path="README.md", content="...")`.

### Update an existing file
1. `github_read_file(connector_id, repo, path)` → old content.
2. Modify content (e.g. apply a code change).
3. `github_update_file(connector_id, repo, path, content=new)`.
