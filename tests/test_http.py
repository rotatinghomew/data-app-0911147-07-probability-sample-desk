from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import tempfile
import threading
import unittest

from app.service import create_server
from app.storage import NoteStore


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = create_server(state_dir=self.temporary.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path, payload=None):
        request = Request(self.base + path, None if payload is None else json.dumps(payload).encode(),
                          {'Content-Type': 'application/json'})
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def test_live_baseline_action(self):
        status, health = self.request('/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['status'], 'ok')
        status, summary = self.request('/api/summary')
        self.assertEqual(status, 200)
        self.assertTrue(summary)
        action = json.loads((Path(__file__).resolve().parents[1] / 'data/default-action.json').read_text())
        status, result = self.request('/api/action', action)
        self.assertEqual(status, 200)
        self.assertTrue(result)

    def test_note_survives_store_reopen(self):
        status, entry = self.request('/api/notes', {'text': '保留当前记录选择', 'selection': {'table': 'records'}})
        self.assertEqual(status, 201)
        self.assertEqual(NoteStore(Path(self.temporary.name)).list(), [entry])
        self.assertEqual(self.request('/api/notes')[1]['notes'], [entry])

    def test_invalid_action_is_client_error(self):
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/action', {'operation': 'unknown'})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_unknown_path_not_treated_as_index(self):
        with self.assertRaises(HTTPError) as caught:
            self.request('/../../etc/passwd')
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_page_assets_available(self):
        for path in ('/', '/app.js', '/style.css'):
            with urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def plan(self, limits):
        status, result = self.request('/api/action', {'operation': 'plan', 'limits': limits})
        self.assertEqual(status, 200)
        return result

    def draw(self, spec, seed=9):
        status, result = self.request('/api/action',
                                      {'operation': 'draw_v2', 'design': spec, 'seed': seed})
        self.assertEqual(status, 200)
        return result

    def test_design_to_estimate_over_http(self):
        plan = self.plan({'minutes': 300, 'minimum_rare_major': 2})
        self.assertTrue(plan['feasible'])
        draw = self.draw(plan['designs'][0]['spec'])
        draw_id = draw['draw_id']
        main = draw['main']
        reserves = {item['class_id']: item['queue'] for item in draw['reserves']}
        status, receipt = self.request('/api/action', {
            'operation': 'receipt', 'draw_id': draw_id,
            'student': main[0]['student'], 'status': 'responded'})
        self.assertEqual(status, 200)
        self.assertEqual(receipt['field']['responded'], 1)
        target = next(entry for entry in main[1:]
                      if entry['cell'] == 'other' and reserves[entry['class_id']])
        self.request('/api/action', {'operation': 'receipt', 'draw_id': draw_id,
                                     'student': target['student'], 'status': 'refused'})
        status, substitution = self.request('/api/action', {
            'operation': 'substitute', 'draw_id': draw_id, 'original': target['student']})
        self.assertEqual(status, 200)
        self.assertEqual(substitution['substitution']['replacement'],
                         reserves[target['class_id']][0])
        self.assertIn('不再等于原概率样本', substitution['probability_note'])
        status, estimate = self.request('/api/action',
                                        {'operation': 'estimate', 'draw_id': draw_id})
        self.assertEqual(status, 200)
        self.assertIsNotNone(estimate['design_based']['ht_total'])
        self.assertTrue(estimate['substitution_view'])
        self.assertEqual(estimate['chain']['seed'], 9)
        status, draws = self.request('/api/action', {'operation': 'draws'})
        self.assertEqual([item['draw_id'] for item in draws['draws']], [draw_id])
        status, detail = self.request('/api/action',
                                      {'operation': 'draw_detail', 'draw_id': draw_id})
        self.assertEqual(detail['main'], main)

    def test_plan_infeasible_returns_conflicts_not_list(self):
        result = self.plan({'minutes': 1, 'minimum_rare_major': 2})
        self.assertFalse(result['feasible'])
        self.assertNotIn('designs', result)
        self.assertTrue(any(c['constraint'] == 'minutes' for c in result['conflicts']))

    def test_field_event_errors_are_client_errors(self):
        plan = self.plan({'minutes': 300, 'minimum_rare_major': 2})
        draw = self.draw(plan['designs'][0]['spec'])
        draw_id = draw['draw_id']
        main = draw['main']
        for payload in (
                {'operation': 'estimate', 'draw_id': 'dr-000000000000'},
                {'operation': 'receipt', 'draw_id': draw_id, 'student': 's-999',
                 'status': 'responded'},
                {'operation': 'receipt', 'draw_id': draw_id, 'student': main[0]['student'],
                 'status': 'unknown'},
                {'operation': 'substitute', 'draw_id': draw_id,
                 'original': main[0]['student']},
                {'operation': 'draw_v2', 'design': plan['designs'][0]['spec'], 'seed': 'x'},
                {'operation': 'draw_v2', 'design': {'strata': []}, 'seed': 1},
        ):
            with self.assertRaises(HTTPError) as caught:
                self.request('/api/action', payload)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        self.request('/api/action', {'operation': 'receipt', 'draw_id': draw_id,
                                     'student': main[0]['student'], 'status': 'out_of_frame'})
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/action', {'operation': 'substitute', 'draw_id': draw_id,
                                         'original': main[0]['student']})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/action', {'operation': 'receipt', 'draw_id': draw_id,
                                         'student': main[0]['student'], 'status': 'refused'})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == '__main__':
    unittest.main()
