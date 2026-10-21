import unittest

from dev_tools.upstream_sync_check import parse_merge_tree


class ParseMergeTreeTest(unittest.TestCase):
    def test_conflict_files_stop_at_blank_line(self):
        output = (
            "5752e0e495c93e1a9e27a7453b605e66e48ca8ea\n"
            "docs/b.md\n"
            "docs/a.md\n"
            "docs/a.md\n"
            "\n"
            "Auto-merging alas.py\n"
            "CONFLICT (content): Merge conflict in docs/a.md\n"
        )
        self.assertEqual(parse_merge_tree(output), ["docs/a.md", "docs/b.md"])

    def test_clean_merge_has_no_conflicts(self):
        self.assertEqual(parse_merge_tree("5752e0e495c93e1a9e27a7453b605e66e48ca8ea\n"), [])


if __name__ == "__main__":
    unittest.main()
