"""Offline tests: real storage class and boto3 import, fake S3 and no bot tokens."""
import gc
import threading
import time
import unittest
import weakref
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from catalog_index import CatalogSnapshot, FilenameIndex
from storage import MemeStorage

ENV = {
    "ENDPOINT": "https://storage.invalid", "ACCESS_KEY_ID": "test",
    "SECRET_ACCESS_KEY": "test", "BUCKET": "test",
    "S3_INDEX_REFRESH_SECONDS": "900",
}


class FakeS3:
    def __init__(self):
        self.keys = [
            "memes/S3香蕉.JPG", "memes/S1海綿.jpg", "memes/S2海綿.png",
            "memes/YN/可以.jpg", "memes/YN/不可以.jpg",
            "airou/cat.jpg", "memes/deep/hidden.jpg", "memes/notes.txt",
        ]
        self.calls = []
        self.fail_prefix = None
        self.fail_after_page = False
        self.block_prefix = None
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.closed = False

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        prefix = kwargs["Prefix"]
        assert kwargs["Delimiter"] == "/"
        assert kwargs["Bucket"] == "test"
        if prefix == self.block_prefix:
            self.entered.set()
            if not self.unblock.wait(3):
                raise TimeoutError("test failed to unblock refresh")
        names = [
            key for key in self.keys
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        ]
        if prefix == self.fail_prefix and not self.fail_after_page:
            raise OSError("simulated bucket outage")
        # Small pages exercise multi-page reads and directory-only pages.
        yield {"CommonPrefixes": [{"Prefix": prefix + "deep/"}]}
        for offset in range(0, len(names), 2):
            yield {"Contents": [{"Key": k} for k in names[offset:offset + 2]]}
            if prefix == self.fail_prefix and self.fail_after_page:
                raise OSError("simulated failure after first data page")

    def close(self):
        self.closed = True


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.env = patch.dict("os.environ", ENV, clear=True)
        self.env.start()
        self.client = patch("storage.boto3.client", return_value=self.s3)
        self.client.start()
        self.storage = MemeStorage()

    def tearDown(self):
        self.s3.unblock.set()
        self.storage.close()
        self.client.stop()
        self.env.stop()

    def test_startup_warms_all_catalogs_once(self):
        self.assertEqual([c["Prefix"] for c in self.s3.calls], ["memes/", "memes/YN/", "airou/"])
        self.assertEqual(self.storage.list_memes(), ("S1海綿.jpg", "S2海綿.png", "S3香蕉.JPG"))
        self.assertEqual(self.storage.list_airou(), ("cat.jpg",))
        self.assertEqual(len(self.storage.list_yn()), 2)

    def test_thousand_searches_and_random_reads_make_zero_list_calls(self):
        calls = len(self.s3.calls)
        memes = self.storage.list_memes()
        airou = self.storage.list_airou()
        for _ in range(1000):
            self.assertIs(self.storage.list_memes(), memes)
            self.assertIs(self.storage.list_airou(), airou)
            self.assertEqual(self.storage.search_memes("海綿"), ["S1海綿.jpg", "S2海綿.png"])
            self.assertEqual(self.storage.search_memes("s3"), ["S3香蕉.JPG"])
            self.assertEqual(len(self.storage.list_yn()), 2)
        self.assertEqual(len(self.s3.calls), calls)

    def test_old_ttl_cannot_force_request_scans(self):
        with patch.dict("os.environ", {"S3_INDEX_TTL": "0"}):
            other = MemeStorage()
            try:
                calls = len(self.s3.calls)
                with patch("time.monotonic", return_value=10**12):
                    for _ in range(20):
                        other.list_memes()
                self.assertEqual(len(self.s3.calls), calls)
            finally:
                other.close()

    def test_yn_remove_does_not_mutate_shared_snapshot(self):
        files = self.storage.list_yn()
        files.remove("可以.jpg")
        self.assertIn("可以.jpg", self.storage.list_yn())

    def test_add_delete_only_visible_after_refresh(self):
        old = self.storage.list_memes()
        self.s3.keys.remove("memes/S1海綿.jpg")
        self.s3.keys.append("memes/S4新增.jpg")
        self.assertIs(self.storage.list_memes(), old)
        self.storage.refresh()
        self.assertNotIn("S1海綿.jpg", self.storage.list_memes())
        self.assertIn("S4新增.jpg", self.storage.list_memes())
        self.assertIn("S1海綿.jpg", old)

    def test_unchanged_refresh_reuses_tuples(self):
        before = self.storage._index.snapshot
        self.storage.refresh()
        after = self.storage._index.snapshot
        self.assertEqual(after.generation, before.generation + 1)
        self.assertIs(after.memes, before.memes)
        self.assertIs(after.yn, before.yn)
        self.assertIs(after.airou, before.airou)

    def test_failure_after_memes_retains_entire_old_generation(self):
        old = self.storage._index.snapshot
        self.s3.keys.append("memes/new.jpg")
        self.s3.fail_prefix = "memes/YN/"
        with self.assertRaises(OSError):
            self.storage.refresh()
        self.assertIs(self.storage._index.snapshot, old)
        self.s3.fail_prefix = None
        self.storage.refresh()
        self.assertIn("new.jpg", self.storage.list_memes())

    def test_pagination_failure_never_publishes_partial_list(self):
        old = self.storage.list_memes()
        self.s3.fail_prefix = "memes/"
        self.s3.fail_after_page = True
        with self.assertRaises(OSError):
            self.storage.refresh()
        self.assertIs(self.storage.list_memes(), old)

    def test_readers_do_not_wait_for_refresh_lock(self):
        old = self.storage.list_memes()
        self.s3.block_prefix = "memes/"
        worker = threading.Thread(target=self.storage.refresh)
        worker.start()
        try:
            self.assertTrue(self.s3.entered.wait(1))
            read_done = threading.Event()
            output = []
            reader = threading.Thread(target=lambda: (output.append(self.storage.list_memes()), read_done.set()))
            reader.start()
            self.assertTrue(read_done.wait(0.5), "reader waited for S3 refresh")
            self.assertIs(output[0], old)
            reader.join(1)
        finally:
            self.s3.unblock.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_empty_successful_listing_replaces_stale_items(self):
        self.s3.keys = []
        self.storage.refresh()
        self.assertEqual(self.storage.list_memes(), ())
        self.assertEqual(self.storage.list_airou(), ())
        calls = len(self.s3.calls)
        self.storage.list_memes()
        self.assertEqual(len(self.s3.calls), calls)

    def test_legacy_airou_fallback(self):
        self.s3.keys = ["Airou/legacy.jpg", "Airou/child/skip.jpg"]
        self.storage.refresh()
        self.assertEqual(self.storage.list_airou(), ("legacy.jpg",))
        self.assertEqual(self.s3.calls[-1]["Prefix"], "Airou/")

    def test_root_and_custom_prefixes(self):
        self.storage.meme_prefix = ""
        self.storage.airou_prefix = "pictures"
        self.s3.keys = ["root.JPG", "YN/yes.jpg", "pictures/pet.png", "nested/no.jpg"]
        self.storage.refresh()
        self.assertEqual(self.storage.list_memes(), ("root.JPG",))
        self.assertEqual(self.storage.list_yn(), ["yes.jpg"])
        self.assertEqual(self.storage.list_airou(), ("pet.png",))

    def test_warmup_failure_does_not_start_worker(self):
        self.s3.fail_prefix = "memes/"
        with patch("catalog_index.FilenameIndex.start") as start:
            with self.assertRaises(OSError):
                MemeStorage()
            start.assert_not_called()
        self.assertTrue(self.s3.closed)

    def test_no_request_ttl_cache_remains(self):
        self.assertFalse(hasattr(self.storage, "_index_cache"))
        self.assertFalse(hasattr(self.storage, "index_ttl"))


class IndexTests(unittest.TestCase):
    def test_cold_reads_raise_instead_of_fetching(self):
        loads = []
        index = FilenameIndex(lambda: loads.append(1))
        with self.assertRaises(RuntimeError):
            _ = index.snapshot
        with self.assertRaises(RuntimeError):
            index.start()
        self.assertEqual(loads, [])
        index.close()

    def test_immutable_snapshot(self):
        index = FilenameIndex(lambda: CatalogSnapshot(memes=("x.jpg",)))
        snap = index.refresh()
        with self.assertRaises(FrozenInstanceError):
            snap.memes = ()
        with self.assertRaises(TypeError):
            snap.memes[0] = "bad.jpg"
        index.close()

    def test_mutable_loader_input_is_defensively_frozen(self):
        values = ["old.jpg"]
        index = FilenameIndex(lambda: CatalogSnapshot(memes=values))
        snap = index.refresh()
        values.append("new.jpg")
        self.assertEqual(snap.memes, ("old.jpg",))
        index.close()

    def test_rejects_invalid_intervals(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FilenameIndex(lambda: CatalogSnapshot(), interval=value)

    def test_background_refresh_without_any_reads(self):
        changed = threading.Event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 2:
                changed.set()
            return CatalogSnapshot(memes=(f"{len(calls)}.jpg",))
        index = FilenameIndex(load, interval=0.02)
        index.refresh()
        index.start()
        try:
            self.assertTrue(changed.wait(2))
        finally:
            index.close()
        self.assertGreaterEqual(index.snapshot.generation, 2)

    def test_background_failure_recovers_and_keeps_old_snapshot(self):
        failed = threading.Event()
        recovered = threading.Event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 2:
                failed.set()
                raise OSError("transient failure")
            if len(calls) >= 3:
                recovered.set()
            return CatalogSnapshot(memes=(f"{len(calls)}.jpg",))
        index = FilenameIndex(load, interval=0.04)
        old = index.refresh()
        index.start()
        try:
            self.assertTrue(failed.wait(2))
            self.assertIs(index.snapshot, old)
            self.assertTrue(recovered.wait(2))
        finally:
            index.close()
        self.assertGreaterEqual(index.snapshot.generation, 2)

    def test_start_is_idempotent(self):
        index = FilenameIndex(lambda: CatalogSnapshot(), interval=900)
        index.refresh()
        index.start()
        worker = index._thread
        for _ in range(10):
            index.start()
            self.assertIs(index._thread, worker)
        self.assertEqual(index.snapshot.generation, 1)
        index.close()
        self.assertFalse(worker.is_alive())

    def test_close_interrupts_long_wait_and_prevents_restart(self):
        index = FilenameIndex(lambda: CatalogSnapshot(), interval=900)
        index.refresh()
        index.start()
        self.assertTrue(index.close())
        with self.assertRaises(RuntimeError):
            index.start()
        with self.assertRaises(RuntimeError):
            index.refresh()
        self.assertTrue(index.close())

    def test_sleeping_worker_does_not_retain_discarded_index(self):
        index = FilenameIndex(lambda: CatalogSnapshot(), interval=900)
        index.refresh()
        index.start()
        worker, stop = index._thread, index._stop
        ref = weakref.ref(index)
        del index
        gc.collect()
        try:
            self.assertIsNone(ref())
        finally:
            stop.set()
            worker.join(1)

    def test_refreshes_are_serialized_but_reads_are_not(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 2:
                entered.set()
                release.wait(2)
            return CatalogSnapshot(memes=(str(len(calls)),))
        index = FilenameIndex(load)
        old = index.refresh()
        first = threading.Thread(target=index.refresh)
        second = threading.Thread(target=index.refresh)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        self.assertIs(index.snapshot, old)
        self.assertEqual(len(calls), 2)
        release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(index.snapshot.generation, 3)
        index.close()


if __name__ == "__main__":
    unittest.main()
