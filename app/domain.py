from collections import Counter
import random

from . import design


def frame_members(data):
    members = {}
    clusters = {row['class_id'] for row in data['clusters']}
    for row in data['frame']:
        if row['student'] in members or row['class_id'] not in clusters:
            raise ValueError('抽样框身份或班级不合法')
        members[row['student']] = row
    return members


def baseline(data):
    members = frame_members(data)
    eligible = [row for row in members.values() if row['eligibility']]
    disposition_ids = set()
    for row in data['dispositions']:
        if row['student'] not in members or row['student'] in disposition_ids:
            raise ValueError('现场回执引用不合法')
        if row['status'] not in ('responded', 'refused', 'absent', 'out_of_frame'):
            raise ValueError('未知现场状态')
        disposition_ids.add(row['student'])
    return {'frame': len(members), 'eligible': len(eligible),
            'majors': dict(Counter(row['major'] for row in eligible)),
            'dispositions': dict(Counter(row['status'] for row in data['dispositions']))}


def act(data, request, log=None):
    if request.get('operation') in design.OPERATIONS:
        return design.handle(data, request, log)
    if request.get('operation') != 'draw':
        raise ValueError('未知抽签操作')
    members = sorted(row['student'] for row in frame_members(data).values() if row['eligibility'])
    size = request.get('size')
    if not isinstance(size, int) or not 0 <= size <= len(members):
        raise ValueError('样本量超出可抽总体')
    seed = request.get('seed')
    if not isinstance(seed, int):
        raise ValueError('种子应为整数')
    selected = sorted(random.Random(seed).sample(members, size))
    probability = size / len(members) if members else None
    return {'design': 'simple_random_without_replacement', 'selected': selected,
            'first_order_probability': probability, 'frame_version': data['frame_version'],
            'notice': '尚未考虑访问成本与专业覆盖；不提供现场便利替补'}
