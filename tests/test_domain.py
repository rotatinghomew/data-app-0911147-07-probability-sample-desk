import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import design, domain
from app.drawlog import DrawLog
from app.service import load_study

ROOT = Path(__file__).resolve().parents[1]


def load_case(name):
    cases = json.loads((ROOT / 'data/analysis-cases.json').read_text())
    return next(case for case in cases if case['name'] == name)['study']


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


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.data = load_study()
        self.limits = dict(self.data['limits'])

    def test_feasible_designs_respect_constraints_and_cover_frame(self):
        result = design.plan(self.data, self.limits)
        self.assertTrue(result['feasible'])
        self.assertTrue(result['designs'])
        for item in result['designs']:
            self.assertLessEqual(item['worst_case_minutes'], self.limits['minutes'])
            self.assertGreaterEqual(item['guaranteed_rare'], self.limits['minimum_rare_major'])
        # 无零入样机会：每个合格学生在首选设计中都有正概率
        spec = result['designs'][0]['spec']
        for row in self.data['frame']:
            if row['eligibility']:
                self.assertGreater(design.first_order(self.data, spec, row['student']), 0)

    def test_budget_conflict_returned_not_silently_relaxed(self):
        result = design.plan(self.data, {'minutes': 100, 'minimum_rare_major': 2})
        self.assertFalse(result['feasible'])
        self.assertNotIn('designs', result)
        constraints = {conflict['constraint'] for conflict in result['conflicts']}
        self.assertIn('minutes', constraints)
        self.assertTrue(result['relaxations'])
        self.assertIn('便利抽样', result['notice'])

    def test_impossible_case_reports_both_conflicts(self):
        study = load_case('impossible')
        result = design.plan(study, study['limits'])
        self.assertFalse(result['feasible'])
        constraints = {conflict['constraint'] for conflict in result['conflicts']}
        self.assertEqual(constraints, {'minutes', 'minimum_rare_major'})
        kinds = {option['kind'] for option in result['relaxations']}
        self.assertIn('lower_coverage', kinds)

    def test_budget_bound_for_estimable_variance_is_honest(self):
        tight = design.plan(self.data, self.limits)
        self.assertTrue(tight['warnings'])
        self.assertFalse(any(d['variance_estimable'] for d in tight['designs']))
        relaxed = design.plan(self.data, {'minutes': 375, 'minimum_rare_major': 2})
        self.assertTrue(any(d['variance_estimable'] for d in relaxed['designs']))

    def test_incomplete_design_rejected(self):
        spec = {'strata': [{'campus': 'north', 'certainty': ['c-0'], 'pool': [], 'sample_count': 0}],
                'other_per_class': 2}
        with self.assertRaises(ValueError):
            design.normalize_spec(self.data, spec)
        # 非定点班抽 0 个会产生零入样机会
        bad = {'strata': [
            {'campus': 'north', 'certainty': [], 'pool': ['c-0', 'c-1', 'c-2'], 'sample_count': 0},
            {'campus': 'south', 'certainty': ['c-3', 'c-4', 'c-5'], 'pool': [], 'sample_count': 0},
        ], 'other_per_class': 2}
        with self.assertRaises(ValueError):
            design.normalize_spec(self.data, bad)


class EnumerationTests(unittest.TestCase):
    """analysis-cases 的六人两班抽样框：枚举全部名单核对一阶、二阶概率与设计方差。"""

    def setUp(self):
        self.data = load_case('enumerable_six')
        self.spec = design.normalize_spec(self.data, {
            'strata': [{'campus': 'north', 'certainty': ['c-0'], 'pool': ['c-1'],
                        'sample_count': 1}],
            'other_per_class': 1,
        })

    def test_closed_form_matches_full_enumeration(self):
        samples = design.enumerate_samples(self.data, self.spec)
        self.assertAlmostEqual(sum(p for _, p in samples), 1.0)
        students = [row['student'] for row in self.data['frame'] if row['eligibility']]
        for student in students:
            empirical = sum(p for sample, p in samples if student in sample)
            self.assertAlmostEqual(design.first_order(self.data, self.spec, student), empirical)
        for first, second in [('s-000', 's-003'), ('s-001', 's-004'), ('s-003', 's-005')]:
            empirical = sum(p for sample, p in samples if first in sample and second in sample)
            self.assertAlmostEqual(design.pair_prob(self.data, self.spec, first, second), empirical)
        # 同层只抽一人时两人同入机会为零
        self.assertEqual(design.pair_prob(self.data, self.spec, 's-003', 's-005'), 0.0)
        values = {row['student']: row['survey_value'] for row in self.data['frame']}
        total = sum(values.values())

        def ht(sample):
            return sum(values[s] / design.first_order(self.data, self.spec, s) for s in sample)

        self.assertAlmostEqual(sum(p * ht(s) for s, p in samples), total)
        variance = sum(p * (ht(s) - total) ** 2 for s, p in samples)
        self.assertAlmostEqual(design.design_variance(self.data, self.spec), variance)

    def test_class_stage_randomization_enumerated(self):
        spec = design.normalize_spec(self.data, {
            'strata': [{'campus': 'north', 'certainty': [], 'pool': ['c-0', 'c-1'],
                        'sample_count': 1}],
            'other_per_class': 2,
        })
        samples = design.enumerate_samples(self.data, spec)
        self.assertAlmostEqual(sum(p for _, p in samples), 1.0)
        # 每班入访概率 1/2；稀少专业学生入样概率即入班概率
        self.assertAlmostEqual(design.first_order(self.data, spec, 's-000'), 0.5)
        self.assertAlmostEqual(design.first_order(self.data, spec, 's-001'), 0.5)
        # 不同班学生同入概率为零（只抽一个班）
        self.assertEqual(design.pair_prob(self.data, spec, 's-000', 's-003'), 0.0)
        estimable, reasons = design.variance_estimable(spec, design.eligible_cells(self.data))
        self.assertFalse(estimable)
        self.assertTrue(reasons)


class DrawAndEstimateTests(unittest.TestCase):
    def setUp(self):
        self.data = load_study()
        self.temporary = tempfile.TemporaryDirectory()
        self.log = DrawLog(self.temporary.name)
        self.plan = design.plan(self.data, dict(self.data['limits']))
        self.spec = self.plan['designs'][0]['spec']

    def tearDown(self):
        self.temporary.cleanup()

    def draw(self, seed=17):
        return design.handle(self.data, {'operation': 'draw_v2', 'design': self.spec,
                                         'seed': seed}, self.log)

    def test_draw_deterministic_and_probabilities_match_design(self):
        first = self.draw()
        second = self.draw()
        self.assertNotEqual(first['draw_id'], second['draw_id'])
        self.assertEqual([m['student'] for m in first['main']],
                         [m['student'] for m in second['main']])
        for entry in first['main']:
            expected = design.first_order(self.data, self.spec, entry['student'])
            self.assertAlmostEqual(entry['pi'], expected, places=5)
            self.assertIn('入样概率', entry['chain'])
        # 定点班的稀少专业学生入样概率为 1
        certainty = {c for stratum in self.spec['strata'] for c in stratum['certainty']}
        rare_certainty = [m for m in first['main']
                          if m['class_id'] in certainty and m['cell'] == 'rare']
        self.assertTrue(rare_certainty)
        for entry in rare_certainty:
            self.assertAlmostEqual(entry['pi'], 1.0)

    def test_infeasible_design_rejected_at_draw(self):
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'draw_v2', 'design': self.spec, 'seed': 1,
                                      'limits': {'minutes': 60, 'minimum_rare_major': 2}},
                          self.log)

    def substitutable(self, draw, exclude=()):
        reserves = {r['class_id']: r['queue'] for r in draw['reserves']}
        return next(m for m in draw['main']
                    if m['cell'] == 'other' and reserves.get(m['class_id'])
                    and m['student'] not in exclude)

    def test_receipt_substitute_estimate_chain(self):
        draw = self.draw()
        draw_id = draw['draw_id']
        main = draw['main']
        values = {row['student']: row['survey_value'] for row in self.data['frame']}
        first = main[0]
        first_pi = design.first_order(self.data, self.spec, first['student'])
        design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                  'student': first['student'], 'status': 'responded'}, self.log)
        target = self.substitutable(draw, exclude={first['student']})
        design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                  'student': target['student'], 'status': 'refused'}, self.log)
        substitution = design.handle(self.data, {'operation': 'substitute', 'draw_id': draw_id,
                                                 'original': target['student']}, self.log)
        replacement = substitution['substitution']['replacement']
        self.assertNotIn(replacement, [m['student'] for m in main])
        estimate = design.handle(self.data, {'operation': 'estimate', 'draw_id': draw_id}, self.log)
        self.assertAlmostEqual(estimate['design_based']['ht_total'],
                               values[first['student']] / first_pi, places=4)
        self.assertEqual(estimate['substitution_view']['substitutes_used'], 1)
        self.assertIn('不是原设计的概率样本',
                      estimate['substitution_view']['probability_basis'])
        # 替补受访后，调整估计按被替者权重计入
        design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                  'student': replacement, 'status': 'responded'}, self.log)
        estimate = design.handle(self.data, {'operation': 'estimate', 'draw_id': draw_id}, self.log)
        target_pi = design.first_order(self.data, self.spec, target['student'])
        expected = values[first['student']] / first_pi + values[replacement] / target_pi
        self.assertAlmostEqual(estimate['substitution_view']['adjusted_total'], expected, places=4)

    def test_out_of_frame_redefines_population_and_blocks_substitution(self):
        draw = self.draw()
        draw_id = draw['draw_id']
        target = draw['main'][0]['student']
        design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                  'student': target, 'status': 'out_of_frame'}, self.log)
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'substitute', 'draw_id': draw_id,
                                      'original': target}, self.log)
        estimate = design.handle(self.data, {'operation': 'estimate', 'draw_id': draw_id}, self.log)
        eligible = sum(1 for row in self.data['frame'] if row['eligibility'])
        self.assertEqual(estimate['population']['size'], eligible - 1)
        self.assertEqual(estimate['population']['out_of_frame_removed'], 1)

    def test_all_refused_yields_no_point_estimate(self):
        study = load_case('all_refused')
        plan = design.plan(study, study['limits'])
        self.assertTrue(plan['feasible'])
        draw = design.handle(study, {'operation': 'draw_v2',
                                     'design': plan['designs'][0]['spec'], 'seed': 3}, self.log)
        for entry in draw['main']:
            design.handle(study, {'operation': 'receipt', 'draw_id': draw['draw_id'],
                                  'student': entry['student'], 'status': 'refused'}, self.log)
        estimate = design.handle(study, {'operation': 'estimate',
                                         'draw_id': draw['draw_id']}, self.log)
        self.assertIsNone(estimate['design_based']['ht_total'])
        self.assertIn('不得以 0 或填补值冒充观测', estimate['design_based']['reason'])
        self.assertEqual(estimate['field']['refused'], estimate['field']['main_size'])

    def test_substitution_rules_enforced(self):
        draw = self.draw()
        draw_id = draw['draw_id']
        pending = draw['main'][0]['student']
        # 无回执不能替补
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'substitute', 'draw_id': draw_id,
                                      'original': pending}, self.log)
        # 稀少专业全取，无同层备用
        rare = next(m for m in draw['main'] if m['cell'] == 'rare')
        design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                  'student': rare['student'], 'status': 'refused'}, self.log)
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'substitute', 'draw_id': draw_id,
                                      'original': rare['student']}, self.log)
        # 重复回执拒绝
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                      'student': rare['student'], 'status': 'responded'}, self.log)
        # 名单外学生拒绝
        with self.assertRaises(ValueError):
            design.handle(self.data, {'operation': 'receipt', 'draw_id': draw_id,
                                      'student': 's-999', 'status': 'absent'}, self.log)

    def test_history_and_detail_traceable(self):
        draw = self.draw()
        draws = design.handle(self.data, {'operation': 'draws'}, self.log)
        self.assertEqual([d['draw_id'] for d in draws['draws']], [draw['draw_id']])
        detail = design.handle(self.data, {'operation': 'draw_detail',
                                           'draw_id': draw['draw_id']}, self.log)
        self.assertEqual(detail['design'], self.spec)
        self.assertEqual(detail['seed'], 17)
        estimate = design.handle(self.data, {'operation': 'estimate',
                                             'draw_id': draw['draw_id']}, self.log)
        self.assertEqual(estimate['chain']['design_id'], draw['design_id'])
        self.assertEqual(estimate['chain']['events'][0]['event'], 'draw')


if __name__ == '__main__':
    unittest.main()
