import unittest
from server.schemas import Position


class TestPositionComparison(unittest.TestCase):
    def setUp(self):
        self.p1 = Position(x=1, y=2)
        self.p2 = Position(x=2, y=2)
        self.p3 = Position(x=1, y=3)
        self.p4 = Position(x=1, y=2)

    def test_lt(self):
        self.assertTrue(self.p1 < self.p2)
        self.assertTrue(self.p1 < self.p3)
        self.assertFalse(self.p2 < self.p1)
        self.assertFalse(self.p3 < self.p1)

    def test_le(self):
        self.assertTrue(self.p1 <= self.p2)
        self.assertTrue(self.p1 <= self.p4)
        self.assertFalse(self.p2 <= self.p1)

    def test_gt(self):
        self.assertTrue(self.p2 > self.p1)
        self.assertTrue(self.p3 > self.p1)
        self.assertFalse(self.p1 > self.p2)

    def test_ge(self):
        self.assertTrue(self.p2 >= self.p1)
        self.assertTrue(self.p1 >= self.p4)
        self.assertFalse(self.p1 >= self.p2)

    def test_eq(self):
        self.assertTrue(self.p1 == self.p4)
        self.assertFalse(self.p1 == self.p2)

    def test_ne(self):
        self.assertTrue(self.p1 != self.p2)
        self.assertFalse(self.p1 != self.p4)

    def test_sort(self):
        positions = [self.p2, self.p3, self.p1]
        sorted_positions = sorted(positions)
        self.assertEqual(sorted_positions, [self.p1, self.p3, self.p2])

    def test_dict_key(self):
        p1 = Position(x=1, y=2)
        p2 = Position(x=2, y=2)
        d = {p1: "A", p2: "B"}
        self.assertEqual(d[p1], "A")
        self.assertEqual(d[p2], "B")


if __name__ == '__main__':
    unittest.main()
