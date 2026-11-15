from pathlib import Path
from urllib.request import Request, urlopen
import json
import tempfile
import threading

from .service import create_server


def main():
    root = Path(__file__).resolve().parents[1]
    request = json.loads((root / 'data/default-action.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        server = create_server(state_dir=directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)

        def act(payload):
            message = Request(base + '/api/action', json.dumps(payload).encode(),
                              {'Content-Type': 'application/json'})
            with urlopen(message, timeout=30) as response:
                return json.load(response)

        try:
            print('本地产品实时运行：' + base)
            for route in ('/health', '/api/summary'):
                with urlopen(base + route, timeout=5) as response:
                    print(route, json.dumps(json.load(response), ensure_ascii=False, indent=2))
            with urlopen(Request(base + '/api/action', json.dumps(request).encode(),
                                 {'Content-Type': 'application/json'}), timeout=5) as response:
                print('现行抽签操作', json.dumps(json.load(response), ensure_ascii=False, indent=2))

            print('—— 多阶段抽样工作流 ——')
            plan = act({'operation': 'plan', 'limits': {'minutes': 300, 'minimum_rare_major': 2}})
            print('可行设计（预算 300）', json.dumps(plan, ensure_ascii=False, indent=2))
            chosen = plan['designs'][0]
            draw = act({'operation': 'draw_v2', 'design': chosen['spec'], 'seed': 17})
            print('正式与备用名单', json.dumps(draw, ensure_ascii=False, indent=2))
            draw_id = draw['draw_id']
            main = draw['main']
            reserves = {item['class_id']: item['queue'] for item in draw['reserves']}
            print('回执：正式名单第一人 responded')
            print(json.dumps(act({'operation': 'receipt', 'draw_id': draw_id,
                                  'student': main[0]['student'], 'status': 'responded'}),
                             ensure_ascii=False, indent=2))
            target = next(entry for entry in main[1:]
                          if entry['cell'] == 'other' and reserves[entry['class_id']])
            print('回执：%s refused，随后启用同层替补' % target['student'])
            print(json.dumps(act({'operation': 'receipt', 'draw_id': draw_id,
                                  'student': target['student'], 'status': 'refused'}),
                             ensure_ascii=False, indent=2))
            print(json.dumps(act({'operation': 'substitute', 'draw_id': draw_id,
                                  'original': target['student']}),
                             ensure_ascii=False, indent=2))
            print('沿原设计估计当前依据')
            print(json.dumps(act({'operation': 'estimate', 'draw_id': draw_id}),
                             ensure_ascii=False, indent=2))
            relaxed = act({'operation': 'plan', 'limits': {'minutes': 375, 'minimum_rare_major': 2}})
            print('放宽预算到 375 后的可行设计（方差可估计性见各项）',
                  json.dumps({'feasible': relaxed['feasible'],
                              'estimable': [d['design_id'] for d in relaxed['designs']
                                            if d['variance_estimable']],
                              'warnings': relaxed.get('warnings')}, ensure_ascii=False, indent=2))
            print('抽签历史', json.dumps(act({'operation': 'draws'}), ensure_ascii=False, indent=2))
            print('运行结束；上述为当前版本的实际响应。')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    main()
