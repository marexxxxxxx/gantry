import unittest

from slotmon.slot import evaluate


class TestHolder(unittest.TestCase):
    def test_oldest_rid_is_holder(self):
        state = evaluate({"aaa": 100.0, "bbb": 300.0, "ccc": 200.0}, waiting=0)
        self.assertEqual(state.holder_rid, "aaa")
        self.assertEqual(state.holder_since, 100.0)

    def test_single_open_rid_is_holder(self):
        state = evaluate({"aaa": 100.0}, waiting=0)
        self.assertEqual(state.holder_rid, "aaa")

    def test_no_open_rid_means_free(self):
        state = evaluate({}, waiting=40)
        self.assertIsNone(state.holder_rid)
        self.assertIsNone(state.holder_since)
        self.assertEqual(state.open_count, 0)


class TestLegacy(unittest.TestCase):
    def test_legacy_is_queue_minus_open(self):
        state = evaluate({"a": 1.0, "b": 2.0, "c": 3.0}, waiting=2301)
        self.assertEqual(state.legacy, 2298)
        self.assertEqual(state.open_count, 3)

    def test_legacy_blocks_the_holder(self):
        state = evaluate({"a": 1.0}, waiting=2301)
        self.assertEqual(state.legacy, 2300)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_zero_legacy_means_complete(self):
        state = evaluate({"a": 1.0}, waiting=1)
        self.assertEqual(state.legacy, 0)
        self.assertEqual(state.holder_rid, "a")
        self.assertTrue(state.complete)

    def test_unknown_waiting_yields_unknown_legacy(self):
        state = evaluate({"a": 1.0}, waiting=None)
        self.assertIsNone(state.legacy)
        self.assertIsNone(state.holder_rid)
        self.assertFalse(state.complete)

    def test_negative_result_is_clamped_to_zero(self):
        state = evaluate({"a": 1.0, "b": 2.0}, waiting=0)
        self.assertEqual(state.legacy, 0)
        self.assertTrue(state.complete)


if __name__ == "__main__":
    unittest.main()