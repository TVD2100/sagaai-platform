# -*- coding: utf-8 -*-
"""
Scenario tests for the REST GitHub connector (github_rest).

Walks the feature the way a user would use it:
  1. Create an encrypted connection and verify its token never leaks.
  2. Create a private repository.
  3. Publish a pack of files in ONE commit via the Git Data API.
  4. Read a file back, update it, delete another file.
  5. Run batch_upsert: unchanged files are skipped, diffs are published, and
     a repeated no-op run performs no commits at all.

All HTTP traffic goes to a stateful in-memory fake GitHub API - no network
access and no real tokens leave the process.
"""
import base64
import hashlib
import json
import urllib.parse
from unittest import mock

import pytest

import core.paths
from core import connectors
from core import github_connector_rest as ghr


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp directory so connectors never touch real data."""
    monkeypatch.setattr(core.paths, "DATA_DIR", str(tmp_path))
    yield tmp_path


def _github_rest_connection(isolated_data_dir, token="ghp_scenario_rest_secret"):
    """Create a github_rest connection; return its public manifest."""
    return connectors.create_connection(
        "github_rest", "GitHub-REST-Scenario", token, account="tvd"
    )


class FakeGitHub:
    """A tiny stateful in-memory GitHub implementing the endpoints the REST
    connector uses. Blobs get their canonical Git SHA-1 so the connector's
    own blob-SHA diffing works against this fake exactly as against GitHub.
    """

    def __init__(self, login="tvd"):
        self.login = login
        self.user = {
            "login": login,
            "name": "Test User",
            "id": 1,
            "public_repos": 0,
            "html_url": f"https://github.com/{login}",
        }
        self.repos = {}       # name -> repo dict
        self.blobs = {}       # blob_sha -> content
        self.trees = {}       # tree_sha -> list of {path, mode, type, sha}
        self.commits = {}     # commit_sha -> {message, tree, parents}
        self.refs = {}        # "refs/heads/main" -> commit_sha
        self._tree_counter = 0
        self._commit_counter = 0
        self.blob_creates = 0
        self.tree_creates = 0
        self.commit_count = 0

    @staticmethod
    def _blob_sha(content):
        """Canonical Git blob SHA-1 for UTF-8 text."""
        data = content.encode("utf-8")
        header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
        return hashlib.sha1(header + data).hexdigest()

    def _new_tree_sha(self):
        self._tree_counter += 1
        return f"t{self._tree_counter:04d}"

    def _new_commit_sha(self):
        self._commit_counter += 1
        return f"c{self._commit_counter:04d}"

    # Low-level dispatch ---------------------------------------------------
    def __call__(self, conn_id, method, url_path, body=None, params=None,
                 timeout=None):
        if url_path == "/user":
            return self.user
        if url_path == "/user/repos" and method == "POST":
            return self._create_repo(body or {})
        if url_path == "/user/repos" and method == "GET":
            repos = list(self.repos.values())
            return [self._repo_view(r) for r in repos]
        if url_path.startswith("/repos/"):
            return self._repo_dispatch(method, url_path, body, params)
        raise AssertionError(f"Unexpected endpoint: {method} {url_path}")

    def _create_repo(self, body):
        name = body.get("name", "")
        if name in self.repos:
            raise ghr.GithubRestError("Repository already exists", status=422)
        repo = {
            "name": name,
            "full_name": f"{self.login}/{name}",
            "private": bool(body.get("private", False)),
            "description": body.get("description", ""),
            "default_branch": "main",
            "owner": {"login": self.login},
        }
        self.repos[name] = repo
        if body.get("auto_init"):
            self._contents_commit(
                self.login, name, "main",
                [{"path": "README.md", "mode": "100644", "type": "blob",
                  "sha": self._blob_sha(f"# {name}\n")}],
                "Initial commit",
            )
        return self._repo_view(repo)

    @staticmethod
    def _repo_view(repo):
        return {
            "full_name": repo["full_name"],
            "name": repo["name"],
            "owner": {"login": repo["owner"]["login"]},
            "private": repo["private"],
            "description": repo.get("description", ""),
            "html_url": f"https://github.com/{repo['full_name']}",
            "default_branch": repo.get("default_branch", "main"),
        }

    def _repo_or_404(self, owner, name):
        repo = self.repos.get(name)
        if repo is None:
            raise ghr.GithubRestError(
                f"Repository {owner}/{name} not found", status=404
            )
        return repo

    def _head_commit(self, repo_name, branch=None):
        ref_name = "refs/heads/" + (branch or "main")
        commit_sha = self.refs.get(ref_name)
        if not commit_sha:
            raise ghr.GithubRestError(
                f"Reference {ref_name} does not exist", status=404
            )
        return ref_name, commit_sha

    def _flat_tree(self, repo_name, branch=None):
        _, commit_sha = self._head_commit(repo_name, branch)
        commit = self.commits[commit_sha]
        return list(self.trees.get(commit["tree"], []))

    def _contents_commit(self, owner, repo_name, branch, entries, message):
        tree_sha = self._new_tree_sha()
        self.trees[tree_sha] = sorted(entries, key=lambda e: e["path"])
        parent = self.refs.get("refs/heads/" + branch)
        commit_sha = self._new_commit_sha()
        self.commits[commit_sha] = {
            "message": message,
            "tree": tree_sha,
            "parents": [parent] if parent else [],
        }
        self.refs["refs/heads/" + branch] = commit_sha
        self.commit_count += 1
        return commit_sha

    def _repo_dispatch(self, method, url_path, body, params):
        parts = url_path.split("/")
        owner, repo_name = parts[2], parts[3]
        rest = "/".join(parts[4:])
        repo = self._repo_or_404(owner, repo_name)

        if rest == "" and method == "GET":
            return self._repo_view(repo)

        if rest.startswith("git/blobs") and method == "POST":
            content = str((body or {}).get("content") or "")
            blob_sha = self._blob_sha(content)
            self.blobs[blob_sha] = content
            self.blob_creates += 1
            return {"sha": blob_sha,
                    "url": f"https://api.github.com/repos/{owner}/{repo_name}/git/blobs/{blob_sha}"}

        if rest == "git/trees" and method == "POST":
            entries = body.get("tree") or []
            base_tree = body.get("base_tree") or ""
            merged = {e["path"]: e for e in (self.trees.get(base_tree) or [])}
            for entry in entries:
                merged[entry["path"]] = entry
            tree_sha = self._new_tree_sha()
            self.trees[tree_sha] = sorted(merged.values(), key=lambda e: e["path"])
            self.tree_creates += 1
            return {"sha": tree_sha, "truncated": False,
                    "url": f"https://api.github.com/repos/{owner}/{repo_name}/git/trees/{tree_sha}"}

        if rest.startswith("git/trees/") and method == "GET":
            tree_sha = urllib.parse.unquote(rest[len("git/trees/"):])
            if tree_sha not in self.trees:
                raise ghr.GithubRestError("Tree not found", status=404)
            return {"sha": tree_sha, "truncated": False, "tree": self.trees[tree_sha]}

        if rest == "git/commits" and method == "POST":
            tree_sha = body.get("tree")
            parents = body.get("parents") or []
            commit_sha = self._new_commit_sha()
            self.commit_count += 1
            message = body.get("message") or "commit"
            commit = {
                "sha": commit_sha,
                "message": message,
                "tree": {"sha": tree_sha},
                "parents": [{"sha": p} for p in parents],
            }
            self.commits[commit_sha] = {
                "message": message,
                "tree": tree_sha,
                "parents": list(parents),
            }
            return commit

        if rest.startswith("git/commits/") and method == "GET":
            commit_sha = urllib.parse.unquote(rest[len("git/commits/"):])
            stored = self.commits.get(commit_sha)
            if stored is None:
                raise ghr.GithubRestError("Commit not found", status=404)
            return {"sha": commit_sha, "message": stored["message"],
                    "tree": {"sha": stored["tree"]},
                    "parents": [{"sha": p} for p in stored["parents"]]}

        if rest == "git/refs" and method == "POST":
            ref_name = body.get("ref", "")
            commit_sha = body.get("sha", "")
            self.refs[ref_name] = commit_sha
            return {"ref": ref_name, "object": {"sha": commit_sha, "type": "commit"}}

        branch_prefixes = ("git/ref/heads/", "git/refs/heads/")
        for prefix in branch_prefixes:
            if rest.startswith(prefix):
                branch = urllib.parse.unquote(rest[len(prefix):])
                return self._branch_dispatch(method, branch, body)

        if rest.startswith("contents/"):
            path = urllib.parse.unquote(rest[len("contents/"):])
            return self._contents_dispatch(method, repo_name, path, body, params)

        raise AssertionError(f"Unexpected repo endpoint: {method} {url_path}")

    def _branch_dispatch(self, method, branch, body):
        ref_name = "refs/heads/" + branch
        if method == "GET":
            commit_sha = self.refs.get(ref_name)
            if not commit_sha:
                raise ghr.GithubRestError(
                    f"Reference {ref_name} does not exist", status=404
                )
            return {"ref": ref_name,
                    "object": {"sha": commit_sha, "type": "commit"}}
        if method == "PATCH":
            commit_sha = body.get("sha", "")
            self.refs[ref_name] = commit_sha
            return {"ref": ref_name,
                    "object": {"sha": commit_sha, "type": "commit"}}
        raise AssertionError(f"Unexpected branch method: {method}")

    def _contents_dispatch(self, method, repo_name, path, body, params):
        branch = urllib.parse.unquote(
            str((body or {}).get("branch") or (params or {}).get("ref") or "main")
        )
        entries = self._flat_tree(repo_name, branch)
        current = next((e for e in entries if e["path"] == path), None)

        if method == "GET":
            if current is None:
                raise ghr.GithubRestError(
                    f"No file named {path} found", status=404
                )
            content = self.blobs[current["sha"]]
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            return {
                "path": path, "sha": current["sha"], "size": len(content),
                "content": encoded, "encoding": "base64",
                "html_url": f"https://github.com/{self.login}/{repo_name}/blob/{branch}/{path}",
            }

        if method == "PUT":
            if current is not None and "sha" not in body:
                raise ghr.GithubRestError("File already exists", status=422)
            if current is not None and body.get("sha") and body["sha"] != current["sha"]:
                raise ghr.GithubRestError("SHA does not match", status=422)
            new_content = base64.b64decode(body["content"]).decode("utf-8")
            blob_sha = self._blob_sha(new_content)
            self.blobs[blob_sha] = new_content
            self.blob_creates += 1
            new_entries = [e for e in entries if e["path"] != path]
            new_entries.append({"path": path, "mode": "100644",
                                "type": "blob", "sha": blob_sha})
            commit_sha = self._contents_commit(
                self.login, repo_name, branch, new_entries,
                body.get("message") or f"Update {path}",
            )
            return {"content": {"sha": blob_sha, "path": path},
                    "commit": {"sha": commit_sha}}

        if method == "DELETE":
            if current is None:
                raise ghr.GithubRestError(
                    f"No file named {path} found", status=404
                )
            if body.get("sha") and body["sha"] != current["sha"]:
                raise ghr.GithubRestError("SHA does not match", status=422)
            new_entries = [e for e in entries if e["path"] != path]
            self._contents_commit(
                self.login, repo_name, branch, new_entries,
                body.get("message") or f"Delete {path}",
            )
            return None

        raise AssertionError(f"Unexpected contents method: {method}")


# ---------------------------------------------------------------------------
# 1. Happy path: publish a pack in one commit, then round-trip and mutate it
# ---------------------------------------------------------------------------

def test_scenario_publish_read_update_delete_batch(isolated_data_dir):
    """Full user journey through the REST connector on a fake GitHub."""
    conn = _github_rest_connection(isolated_data_dir)
    conn_id = conn["id"]
    assert conn["service"] == "github_rest"
    assert conn["has_token"] is True
    assert "ghp_scenario_rest_secret" not in json.dumps([conn])

    repo = "tvd/scenario-rest-repo"
    fake = FakeGitHub()

    with mock.patch.object(ghr, "_request", new=fake):
        # 1. Connection validates and refreshes the stored account.
        result = ghr.test_connection(conn_id)
        assert result["ok"] is True
        assert result["login"] == "tvd"
        assert connectors.get_connection(conn_id)["account"] == "tvd"

        # 2. Private repository creation.
        created = ghr.create_repo(
            conn_id, "scenario-rest-repo", description="REST scenario",
            private=True, auto_init=False,
        )
        assert created["full_name"] == repo
        assert created["private"] is True
        assert ghr.get_repo_info(conn_id, repo)["private"] is True

        # 3. Publish the whole pack in ONE commit (the optimization).
        files = [
            {"path": "README.md",
             "content": "# Scenario\n\nREST connector test repo.\n"},
            {"path": "src/main.py", "content": 'print("hello rest")\n'},
        ]
        batch = ghr.batch_commit(
            conn_id, repo, files, message="Initial batch", branch="main"
        )
        assert batch["committed"] is True
        assert batch["files_created"] == 2
        assert batch["total_files"] == 2
        assert batch["ref_created"] is True
        assert batch["commit_sha"]
        # Exactly one commit and one tree for the whole pack.
        assert fake.commit_count == 1
        assert fake.tree_creates == 1
        assert fake.blob_creates == 2

        # 4. Read a file back through the Contents API and the tool layer.
        read = ghr.read_file(conn_id, repo, "README.md", branch="main")
        assert read["content"] == files[0]["content"]
        remote_sha = read["sha"]

        from core import github_tools_rest
        tool_read = github_tools_rest.ghr_read_file(
            connector_id=conn_id, repo=repo, path="README.md", branch="main"
        )
        assert tool_read["ok"] is True
        assert tool_read["result"]["content"] == files[0]["content"]

        # 5. Update the file; the next read sees the new content.
        updated_content = "# Scenario v2\n"
        updated = ghr.update_file(
            conn_id, repo, "README.md", updated_content,
            message="Update README", branch="main", sha=remote_sha,
        )
        assert updated["committed"] is True
        assert ghr.read_file(conn_id, repo, "README.md", branch="main")["content"] == updated_content

        # 6. Delete the second file; reading it back raises a clean 404 error.
        src_sha = ghr.read_file_meta(
            conn_id, repo, "src/main.py", branch="main"
        )["sha"]
        deleted = ghr.delete_file(
            conn_id, repo, "src/main.py", message="Remove main",
            branch="main", sha=src_sha,
        )
        assert deleted == {"path": "src/main.py", "committed": True}
        with pytest.raises(ghr.GithubRestError):
            ghr.read_file(conn_id, repo, "src/main.py", branch="main")

        # 7. batch_upsert diffs by blob SHA: README unchanged, two files created.
        snapshot = [
            {"path": "README.md", "content": updated_content},
            {"path": "src/main.py", "content": 'print("hello rest")\n'},
            {"path": "docs/notes.txt", "content": "notes\n"},
        ]
        commits_before = fake.commit_count
        upsert = ghr.batch_upsert(
            conn_id, repo, snapshot, message="Upsert", branch="main"
        )
        assert upsert["committed"] is True
        assert upsert["files_unchanged"] == 1
        assert upsert["files_created"] == 2
        assert fake.commit_count == commits_before + 1

        # 8. Re-running the same snapshot performs no commit at all.
        commits_before = fake.commit_count
        blobs_before = fake.blob_creates
        upsert_again = ghr.batch_upsert(
            conn_id, repo, snapshot, message="Upsert again", branch="main"
        )
        assert upsert_again["committed"] is False
        assert upsert_again["files_unchanged"] == 3
        assert fake.commit_count == commits_before
        assert fake.blob_creates == blobs_before

        # 9. The remote tree matches the local snapshot by canonical blob SHA.
        tree = ghr.get_tree(conn_id, repo, branch="main", recursive=True)
        remote_paths = {
            e["path"] for e in tree["entries"] if e["type"] == "blob"
        }
        assert remote_paths == {"README.md", "src/main.py", "docs/notes.txt"}
        readme_entry = next(
            e for e in tree["entries"] if e["path"] == "README.md"
        )
        assert readme_entry["sha"] == ghr._git_blob_sha(updated_content)


# ---------------------------------------------------------------------------
# 2. Credential and publish edge cases
# ---------------------------------------------------------------------------

def test_scenario_upload_over_existing_file_fails_cleanly(isolated_data_dir):
    """Uploading a file that already exists reports 'already exists'."""
    conn = _github_rest_connection(isolated_data_dir)
    conn_id = conn["id"]
    repo = "tvd/scenario-conflict-repo"
    fake = FakeGitHub()

    with mock.patch.object(ghr, "_request", new=fake):
        ghr.create_repo(conn_id, "scenario-conflict-repo", private=True,
                        auto_init=False)
        ghr.batch_commit(
            conn_id, repo, [{"path": "a.txt", "content": "one\n"}],
            branch="main",
        )
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.upload_file(conn_id, repo, "a.txt", "two\n", branch="main")
    assert "already exists" in str(exc_info.value)


def test_scenario_created_repo_appears_in_listing(isolated_data_dir):
    """A freshly created repo is visible via list_repos."""
    conn = _github_rest_connection(isolated_data_dir)
    conn_id = conn["id"]
    fake = FakeGitHub()

    with mock.patch.object(ghr, "_request", new=fake):
        ghr.create_repo(conn_id, "scenario-list-repo", description="listed",
                        private=True, auto_init=False)
        repos = ghr.list_repos(conn_id)
    names = [r["name"] for r in repos]
    assert "scenario-list-repo" in names
    listed = next(r for r in repos if r["name"] == "scenario-list-repo")
    assert listed["private"] is True
    assert listed["description"] == "listed"


def test_scenario_missing_repo_and_file_report_clean_errors(isolated_data_dir):
    """Accessing a missing repo/file yields GithubRestError with status 404."""
    conn = _github_rest_connection(isolated_data_dir)
    conn_id = conn["id"]
    fake = FakeGitHub()

    with mock.patch.object(ghr, "_request", new=fake):
        with pytest.raises(ghr.GithubRestError) as exc_info:
            ghr.get_repo_info(conn_id, "tvd/does-not-exist")
        assert exc_info.value.status == 404

        ghr.create_repo(conn_id, "scenario-missing-file", private=True,
                        auto_init=False)
        ghr.batch_commit(
            conn_id, "tvd/scenario-missing-file",
            [{"path": "present.txt", "content": "x\n"}], branch="main",
        )
        with pytest.raises(ghr.GithubRestError) as exc_info_2:
            ghr.read_file(
                conn_id, "tvd/scenario-missing-file", "absent.txt", branch="main"
            )
        assert exc_info_2.value.status == 404


def test_scenario_tool_layer_returns_error_dicts_for_failures(isolated_data_dir):
    """The orchestrator-facing tools never raise: failures become dicts."""
    conn = _github_rest_connection(isolated_data_dir)
    conn_id = conn["id"]
    fake = FakeGitHub()

    from core import github_tools_rest
    with mock.patch.object(ghr, "_request", new=fake):
        ghr.create_repo(conn_id, "scenario-tool-errors", private=True,
                        auto_init=False)
        ghr.batch_commit(
            conn_id, "tvd/scenario-tool-errors",
            [{"path": "present.txt", "content": "x\n"}], branch="main",
        )
        result = github_tools_rest.ghr_delete_file(
            connector_id=conn_id, repo="tvd/scenario-tool-errors",
            path="not-there.txt", branch="main",
        )
    assert result["ok"] is False
    assert "not-there.txt" in result["error"]
