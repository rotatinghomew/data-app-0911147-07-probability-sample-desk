import copy
import unittest
from app import domain
from app.service import load_study


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.data = load_study()
        self.request = {'operation': 'draw', 'size': 8, 'seed': 17}

    def test_seed_replay_without_duplicates(self):
        first = domain.act(self.data, self.request)
        self.assertEqual(first, domain.act(self.data, self.request))
        self.assertEqual(len(first['selected']), len(set(first['selected'])))

    def test_known_sampling_fraction(self):
        data = copy.deepcopy(self.data)
        data['frame'] = [dict(row, eligibility=True) for row in data['frame'][:6]]
        result = domain.act(data, {'operation': 'draw', 'size': 2, 'seed': 7})
        self.assertAlmostEqual(result['first_order_probability'], 1 / 3)

    def test_overdrawing_is_not_silently_truncated(self):
        with self.assertRaises(ValueError):
            domain.act(self.data, {'operation': 'draw', 'size': 100000, 'seed': 7})


if __name__ == '__main__':
    unittest.main()
