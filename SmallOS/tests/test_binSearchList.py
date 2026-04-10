import sys
import unittest

sys.path.append("..")

from SmallPackage.list_util.binSearchList import insert, search


class Element:
    def __init__(self, num):
        self.num = num


class TestBinSearchList(unittest.TestCase):
    def test_search_out_of_bounds_raises(self):
        data = list(range(100))

        with self.assertRaises(IndexError):
            search(data, 100, 0, len(data) + 1)

        with self.assertRaises(IndexError):
            search(data, -1, -1, len(data))

    def test_search_finds_existing_items(self):
        data = list(range(100))

        for target in data:
            index = search(data, target, 0, len(data))
            self.assertEqual(target, data[index])

    def test_search_returns_missing_for_unknown_item(self):
        data = list(range(100))
        self.assertEqual(-1, search(data, 500, 0, len(data)))

    def test_search_supports_custom_extractor(self):
        data = [Element(num) for num in range(100)]
        extractor = lambda items, index: items[index].num

        for target in range(100):
            index = search(data, target, 0, len(data), extractor)
            self.assertNotEqual(-1, index)
            self.assertEqual(target, data[index].num)

    def test_insert_out_of_bounds_raises(self):
        data = list(range(100))

        with self.assertRaises(IndexError):
            insert(data, 100, 0, len(data) + 1)

        with self.assertRaises(IndexError):
            insert(data, -1, -1, len(data))

    def test_insert_returns_sorted_insertion_point(self):
        data = [0, 2, 4, 6]

        self.assertEqual(0, insert(data, -1, 0, len(data)))
        self.assertEqual(2, insert(data, 3, 0, len(data)))
        self.assertEqual(len(data), insert(data, 10, 0, len(data)))


if __name__ == "__main__":
    unittest.main()
