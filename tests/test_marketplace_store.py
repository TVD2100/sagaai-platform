"""
test_marketplace_store.py - layout, mutex, atomic writes and caches.

The store is the piece every other module trusts: if it silently loses a
write or lets two runs interleave, a rollback would restore the wrong
state. These tests pin the mutex (including the thread case a plain file
lock misses), the atomic write seam and the cache eviction rules.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.marketplace_fixtures import marketplace_data_dir  # noqa: F401,E402


class TestLayout:
    def test_ensure_layout_creates_every_folder(self, marketplace_data_dir):
        from core.marketplace import store

        root = Path(store.ensure_layout())
        assert root.is_dir()
        for relative in ("staging", "backup", "cache/packages", "cache/entities"):
            assert (root / relative).is_dir(), relative

    def test_all_paths_live_under_the_data_dir(self, marketplace_data_dir):
        from core.marketplace import store

        root = store.marketplace_root()
        for path in (store.registry_cache_path(), store.history_path(),
                     store.state_path(), store.lock_path(),
                     store.packages_cache_dir()):
            assert path.startswith(root)


class TestAtomicWrites:
    def test_atomic_write_json_creates_file(self, marketplace_data_dir):
        from core.marketplace import store

        target = Path(marketplace_data_dir) / "payload.json"
        store.atomic_write_json(str(target), {"a": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_failed_replace_keeps_the_old_content(self, marketplace_data_dir, monkeypatch):
        from core.marketplace import store

        target = Path(marketplace_data_dir) / "payload.json"
        store.atomic_write_json(str(target), {"a": 1})

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(store, "_atomic_replace", _boom)
        with pytest.raises(OSError):
            store.atomic_write_json(str(target), {"a": 2})
        # The previous file must still be readable and unchanged.
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_failed_replace_leaves_no_temp_file(self, marketplace_data_dir, monkeypatch):
        from core.marketplace import store

        target = Path(marketplace_data_dir) / "payload.json"

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(store, "_atomic_replace", _boom)
        with pytest.raises(OSError):
            store.atomic_write_json(str(target), {"a": 2})
        leftovers = [p.name for p in Path(marketplace_data_dir).iterdir() if p.name.startswith(".tmp-")]
        assert leftovers == []

    def test_read_json_returns_default_on_garbage(self, marketplace_data_dir):
        from core.marketplace import store

        target = Path(marketplace_data_dir) / "broken.json"
        target.write_text("{not json", encoding="utf-8")
        assert store.read_json(str(target), default={"fallback": True}) == {"fallback": True}


class TestInstallLock:
    def test_second_acquisition_raises(self, marketplace_data_dir):
        from core.marketplace import store

        with store.install_lock():
            with pytest.raises(store.MarketplaceBusyError):
                with store.install_lock():
                    pass

    def test_lock_is_released_after_the_block(self, marketplace_data_dir):
        from core.marketplace import store

        with store.install_lock():
            pass
        assert store.is_locked() is False
        with store.install_lock():
            assert store.is_locked() is True

    def test_lock_is_released_after_an_exception(self, marketplace_data_dir):
        from core.marketplace import store

        with pytest.raises(RuntimeError):
            with store.install_lock():
                raise RuntimeError("boom")
        with store.install_lock():
            pass

    def test_threads_do_not_both_win(self, marketplace_data_dir):
        from core.marketplace import store

        # A file lock alone does not stop two threads of the same process:
        # each opens its own descriptor. The in-process guard must.
        results = []
        barrier = threading.Barrier(2)

        def _worker():
            barrier.wait()
            try:
                with store.install_lock():
                    time.sleep(0.05)
                results.append("ok")
            except store.MarketplaceBusyError:
                results.append("busy")

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(results) == ["busy", "ok"]


class TestInstallId:
    def test_format(self, marketplace_data_dir):
        from core.marketplace import store

        install_id = store.new_install_id()
        # Fixed format: it names the backup folder and appears in the log.
        assert len(install_id) == len("20261027T100000Z-aaaa")
        assert install_id[8] == "T" and install_id[15] == "Z" and install_id[16] == "-"

    def test_ids_are_unique(self, marketplace_data_dir):
        from core.marketplace import store

        ids = {store.new_install_id() for _ in range(20)}
        assert len(ids) == 20


class TestPackageCache:
    def _fake_packages(self, count):
        return [(f"{index:064x}", f"package-{index}".encode()) for index in range(count)]

    def test_cache_and_read_back(self, marketplace_data_dir):
        from core.marketplace import store

        sha, data = self._fake_packages(1)[0]
        path = store.cache_package(sha, data)
        assert Path(path).is_file()
        assert store.get_cached_package(sha) == data

    def test_missing_package_returns_none(self, marketplace_data_dir):
        from core.marketplace import store

        assert store.get_cached_package("0" * 64) is None

    def test_expired_package_is_evicted(self, marketplace_data_dir, monkeypatch):
        from datetime import timedelta

        from core.marketplace import store

        sha, data = self._fake_packages(1)[0]
        store.cache_package(sha, data)

        # Jump 31 days ahead: the TTL is 30.
        real_now = store._now
        monkeypatch.setattr(store, "_now", lambda: real_now() + timedelta(days=31))
        assert store.get_cached_package(sha) is None

    def test_pinned_package_survives_ttl(self, marketplace_data_dir, monkeypatch):
        from datetime import timedelta

        from core.marketplace import store

        sha, data = self._fake_packages(1)[0]
        store.cache_package(sha, data)
        store.pin_package(sha)
        real_now = store._now
        monkeypatch.setattr(store, "_now", lambda: real_now() + timedelta(days=31))
        assert store.get_cached_package(sha) == data

    def test_lru_prunes_oldest_unpinned(self, marketplace_data_dir, monkeypatch):
        from datetime import timedelta

        from core.marketplace import store

        packages = self._fake_packages(4)
        real_now = store._now
        base = real_now()
        for index, (sha, data) in enumerate(packages):
            monkeypatch.setattr(store, "_now", lambda offset=index: base + timedelta(minutes=offset))
            store.cache_package(sha, data)
        monkeypatch.setattr(store, "_now", real_now)

        evicted = store.prune_cache(max_bytes=len(packages[0][1]) * 2)
        assert len(evicted) == 2
        # The oldest two go first; the newest two stay.
        assert store.get_cached_package(packages[0][0]) is None
        assert store.get_cached_package(packages[3][0]) is not None

    def test_prune_keeps_pinned_entries(self, marketplace_data_dir):
        from core.marketplace import store

        packages = self._fake_packages(2)
        for sha, data in packages:
            store.cache_package(sha, data)
        store.pin_package(packages[0][0])
        evicted = store.prune_cache(max_bytes=1)
        assert packages[0][0] not in evicted
        assert store.get_cached_package(packages[0][0]) is not None


class TestRunLog:
    def test_append_and_find(self, marketplace_data_dir):
        from core.marketplace import store

        run = store.append_run({"type": "skill", "id": "demo", "action": "install"})
        assert run["install_id"]
        assert run["status"] == "applying"
        found = store.find_run(run["install_id"])
        assert found is not None and found["id"] == "demo"

    def test_update_run(self, marketplace_data_dir):
        from core.marketplace import store

        run = store.append_run({"type": "skill", "id": "demo"})
        updated = store.update_run(run["install_id"], status="done")
        assert updated["status"] == "done"
        assert store.applying_runs() == []

    def test_retention_keeps_twenty_skills(self, marketplace_data_dir):
        from core.marketplace import store

        for index in range(25):
            store.append_run({"type": "skill", "id": f"skill_{index}"})
        records = store.load_history()
        assert len(records) == 20
        assert records[-1]["id"] == "skill_24"

    def test_retention_keeps_three_rag_runs(self, marketplace_data_dir):
        from core.marketplace import store

        for index in range(5):
            store.append_run({"type": "rag", "id": f"base_{index}"})
        records = store.load_history()
        assert len(records) == 3
        assert records[-1]["id"] == "base_4"

    def test_state_tracks_the_active_run(self, marketplace_data_dir):
        from core.marketplace import store

        run = store.append_run({"type": "skill", "id": "demo"})
        assert store.get_state()["install_id"] == run["install_id"]
        store.clear_state()
        assert store.get_state() is None


class TestRunDirs:
    def test_prepare_cleans_previous_attempt(self, marketplace_data_dir):
        from core.marketplace import store

        staging, backup = store.prepare_run_dirs("20261027T100000Z-aaaa")
        Path(staging, "leftover.txt").write_text("x", encoding="utf-8")
        staging, backup = store.prepare_run_dirs("20261027T100000Z-aaaa")
        assert not Path(staging, "leftover.txt").exists()

    def test_cleanup_keeps_backup_by_default(self, marketplace_data_dir):
        from core.marketplace import store

        install_id = "20261027T100000Z-bbbb"
        staging, backup = store.prepare_run_dirs(install_id)
        Path(staging, "x.txt").write_text("x", encoding="utf-8")
        store.cleanup_run_dirs(install_id)
        assert not Path(staging).exists()
        assert Path(backup).exists()

    def test_run_meta_round_trip(self, marketplace_data_dir):
        from core.marketplace import store

        install_id = "20261027T100000Z-cccc"
        store.prepare_run_dirs(install_id)
        store.write_run_meta(install_id, {"install_id": install_id, "type": "skill"})
        assert store.read_run_meta(install_id)["type"] == "skill"
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
