import unittest

from scripts.find_orphan_knowledge_folders import folder_name_for_title


class KnowledgeFolderNameTest(unittest.TestCase):
    def test_windows_characters_whitespace_and_truncation(self) -> None:
        self.assertEqual(folder_name_for_title("API 5UE - Go/Go+?"), "API 5UE - Go_Go+_")
        self.assertEqual(folder_name_for_title("A   B."), "A B")
        self.assertEqual(folder_name_for_title("x" * 70), "x" * 60)


if __name__ == "__main__":
    unittest.main()
