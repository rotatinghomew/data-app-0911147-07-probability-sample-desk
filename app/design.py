"""多阶段概率抽样：设计、抽签、现场处置与估计的贯通逻辑。

设计结构：校区分层 → 班级阶段（定点班必访 + 待抽班中简单随机抽 m 个）
→ 班内按稀少/非稀少专业分层（稀少专业全取，非稀少专业简单随机抽 n 个）。
抽签、现场回执、替补与估计共享本模块的同一份概率语义；
任何一步都不会把便利处置伪装成概率样本。
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations, product
import hashlib
import json
import random
import uuid

CELL_RARE = 'rare'
CELL_OTHER = 'other'
FIELD_STATUSES = ('responded', 'refused', 'absent', 'out_of_frame')
SUBSTITUTABLE = ('refused', 'absent')
OPERATIONS = ('plan', 'draw_v2', 'receipt', 'substitute', 'estimate', 'draws', 'draw_detail')
MAX_DESIGNS_KEPT = 12


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------- 抽样框 ----------

def class_table(data):
    table = {}
    for row in data['clusters']:
        table[row['class_id']] = {
            'campus': row['campus'],
            'visit_cost': row['visit_cost'],
            'interview_minutes': row['interview_minutes'],
        }
    return table


def frame_rows(data):
    members = {}
    classes = class_table(data)
    for row in data['frame']:
        if row['student'] in members or row['class_id'] not in classes:
            raise ValueError('抽样框身份或班级不合法')
        members[row['student']] = row
    return members


def eligible_cells(data):
    """每个班级内合格学生按稀少/非稀少专业分层，名单排序固定。"""
    cells = {c: {CELL_RARE: [], CELL_OTHER: []} for c in class_table(data)}
    for row in frame_rows(data).values():
        if not row['eligibility']:
            continue
        cell = CELL_RARE if row['major'] == 'rare' else CELL_OTHER
        cells[row['class_id']][cell].append(row['student'])
    for groups in cells.values():
        groups[CELL_RARE].sort()
        groups[CELL_OTHER].sort()
    return cells


def validate_limits(raw):
    if not isinstance(raw, dict):
        raise ValueError('约束应为对象')
    minutes = raw.get('minutes')
    required = raw.get('minimum_rare_major')
    if not isinstance(minutes, int) or minutes <= 0:
        raise ValueError('访谈工时预算应为正整数')
    if not isinstance(required, int) or required < 0:
        raise ValueError('稀少专业覆盖要求应为非负整数')
    return {'minutes': minutes, 'minimum_rare_major': required}


# ---------- 设计结构 ----------

def normalize_spec(data, spec):
    """校验并规范化设计；不完整或会悄悄缩小总体的设计直接拒绝。"""
    if not isinstance(spec, dict):
        raise ValueError('设计应为对象')
    table = class_table(data)
    strata = spec.get('strata')
    take = spec.get('other_per_class')
    if not isinstance(strata, list) or not strata:
        raise ValueError('设计缺少校区分层')
    if not isinstance(take, int) or take < 1:
        raise ValueError('每班非稀少专业抽样数应为正整数')
    seen = {}
    normalized = []
    for stratum in sorted(strata, key=lambda item: str(item.get('campus', ''))):
        campus = stratum.get('campus')
        certainty = sorted(stratum.get('certainty') or [])
        pool = sorted(stratum.get('pool') or [])
        count = stratum.get('sample_count')
        if not isinstance(count, int) or not 0 <= count <= len(pool):
            raise ValueError('校区 %s 的抽班数不合法' % campus)
        if pool and count < 1:
            raise ValueError('校区 %s 存在非定点班却抽 0 班，会产生零入样机会' % campus)
        if set(certainty) & set(pool):
            raise ValueError('班级不能同时定点与待抽')
        for class_id in certainty + pool:
            if class_id not in table:
                raise ValueError('设计引用未知班级 ' + str(class_id))
            if table[class_id]['campus'] != campus:
                raise ValueError('班级 %s 不属于校区 %s' % (class_id, campus))
            if class_id in seen:
                raise ValueError('班级 %s 在设计中重复出现' % class_id)
            seen[class_id] = campus
        normalized.append({'campus': campus, 'certainty': certainty, 'pool': pool,
                           'sample_count': count})
    if set(seen) != set(table):
        raise ValueError('设计必须覆盖抽样框中的全部班级，不允许悄悄缩小总体')
    return {'strata': normalized, 'other_per_class': take}


def spec_id(spec):
    blob = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    return 'd-' + hashlib.sha1(blob.encode('utf-8')).hexdigest()[:10]


def spec_summary(spec):
    parts = []
    for stratum in spec['strata']:
        bits = []
        if stratum['certainty']:
            bits.append('定点[%s]' % '、'.join(stratum['certainty']))
        if stratum['pool']:
            bits.append('%d 个待抽班中抽 %d 个' % (len(stratum['pool']), stratum['sample_count']))
        parts.append('%s：%s' % (stratum['campus'], '，'.join(bits)))
    parts.append('班内稀少专业全取、非稀少专业每班抽 %d 人' % spec['other_per_class'])
    return '；'.join(parts)


def _stratum_of(spec, class_id):
    for stratum in spec['strata']:
        if class_id in stratum['certainty'] or class_id in stratum['pool']:
            return stratum
    raise KeyError(class_id)


def class_visit_prob(spec, class_id):
    stratum = _stratum_of(spec, class_id)
    if class_id in stratum['certainty']:
        return 1.0
    return stratum['sample_count'] / len(stratum['pool'])


def class_co_visit_prob(spec, first, second):
    sa, sb = _stratum_of(spec, first), _stratum_of(spec, second)
    if sa['campus'] != sb['campus']:
        return class_visit_prob(spec, first) * class_visit_prob(spec, second)
    a_cert = first in sa['certainty']
    b_cert = second in sa['certainty']
    if a_cert and b_cert:
        return 1.0
    count, size = sa['sample_count'], len(sa['pool'])
    if a_cert != b_cert:
        return count / size
    if count < 2:
        return 0.0
    return count * (count - 1) / (size * (size - 1))


class _DesignContext:
    """同一设计下的一阶、二阶入样概率，供抽签说明与估计共用。"""

    def __init__(self, data, spec):
        self.spec = spec
        self.members = frame_rows(data)
        self.cells = eligible_cells(data)
        self.cell_of = {s: (CELL_RARE if row['major'] == 'rare' else CELL_OTHER)
                        for s, row in self.members.items()}
        self.visit = {}
        for stratum in spec['strata']:
            for class_id in stratum['certainty']:
                self.visit[class_id] = 1.0
            count, size = stratum['sample_count'], len(stratum['pool'])
            for class_id in stratum['pool']:
                self.visit[class_id] = count / size
        self.within = {}
        for class_id, groups in self.cells.items():
            rare = groups[CELL_RARE]
            others = groups[CELL_OTHER]
            take = min(spec['other_per_class'], len(others))
            self.within[(class_id, CELL_RARE)] = 1.0 if rare else None
            self.within[(class_id, CELL_OTHER)] = take / len(others) if others else None

    def pi(self, student):
        row = self.members[student]
        return self.visit[row['class_id']] * self.within[(row['class_id'], self.cell_of[student])]

    def pijk(self, first, second):
        if first == second:
            return self.pi(first)
        ra, rb = self.members[first], self.members[second]
        ca, cb = ra['class_id'], rb['class_id']
        pa = self.within[(ca, self.cell_of[first])]
        pb = self.within[(cb, self.cell_of[second])]
        if ca != cb:
            return class_co_visit_prob(self.spec, ca, cb) * pa * pb
        cell_a, cell_b = self.cell_of[first], self.cell_of[second]
        if cell_a == CELL_RARE and cell_b == CELL_RARE:
            within = 1.0
        elif cell_a != cell_b:
            within = pa * pb
        else:
            others = self.cells[ca][CELL_OTHER]
            take = min(self.spec['other_per_class'], len(others))
            size = len(others)
            within = take * (take - 1) / (size * (size - 1)) if size >= 2 else 0.0
        return self.visit[ca] * within


def first_order(data, spec, student):
    return _DesignContext(data, spec).pi(student)


def pair_prob(data, spec, first, second):
    return _DesignContext(data, spec).pijk(first, second)


def variance_estimable(spec, cells):
    """设计无偏方差是否可估计：班间成分需要每校区抽班数不为 1（除非即全部），
    层内成分需要非稀少专业层抽样数不为 1（除非即全部）。"""
    reasons = []
    for stratum in spec['strata']:
        count, size = stratum['sample_count'], len(stratum['pool'])
        if count == 1 and size > 1:
            reasons.append('校区 %s 在 %d 个待抽班中仅抽 1 班，班间方差成分无设计无偏估计'
                           % (stratum['campus'], size))
    thin = [c for c, groups in cells.items()
            if len(groups[CELL_OTHER]) >= 2
            and min(spec['other_per_class'], len(groups[CELL_OTHER])) < 2]
    if thin:
        reasons.append('班级 %s 的非稀少专业层仅抽 1 人，层内方差无设计无偏估计' % '、'.join(thin))
    return (not reasons, reasons)


def design_variance(data, spec):
    """HT 总量估计的精确设计方差（用抽样框中的 survey_value 作规划基准）。

    按校区分解：Var = Σ_i [q_i·S1_i − q_i²·A_i²] + Σ_{i≠i'} (q_{ii'} − q_i·q_{i'})·A_i·A_{i'}，
    其中 A_i = Σ_{j∈i} p_j·z_j，S1_i = Σ_{j,k∈i} w_{jk}·z_j·z_k，z_j = y_j/π_j。
    """
    spec = normalize_spec(data, spec)
    ctx = _DesignContext(data, spec)
    values = {s: row['survey_value'] for s, row in ctx.members.items()}
    total = 0.0
    for stratum in spec['strata']:
        classes = sorted(stratum['certainty'] + stratum['pool'])
        aggregate = {}
        within_sum = {}
        for class_id in classes:
            groups = ctx.cells[class_id]
            z_rare = [values[s] / ctx.pi(s) for s in groups[CELL_RARE]]
            others = groups[CELL_OTHER]
            take = min(spec['other_per_class'], len(others))
            size = len(others)
            z_other = [values[s] / ctx.pi(s) for s in others]
            p_other = take / size if size else 0.0
            aggregate[class_id] = sum(z_rare) + p_other * sum(z_other)
            s1 = sum(z_rare) ** 2 if z_rare else 0.0
            if size:
                sum_z = sum(z_other)
                sum_z2 = sum(z * z for z in z_other)
                s1 += p_other * sum_z2
                if size >= 2:
                    pair = take * (take - 1) / (size * (size - 1))
                    s1 += pair * (sum_z ** 2 - sum_z2)
                s1 += 2 * sum(z_rare) * p_other * sum_z
            within_sum[class_id] = s1
        for class_id in classes:
            q = ctx.visit[class_id]
            total += q * within_sum[class_id] - q * q * aggregate[class_id] ** 2
        for index, first in enumerate(classes):
            for second in classes[:index]:
                co = class_co_visit_prob(spec, first, second)
                total += 2 * (co - ctx.visit[first] * ctx.visit[second]) \
                    * aggregate[first] * aggregate[second]
    return total


# ---------- 可行设计搜索 ----------

def candidate_specs(data, limits):
    """枚举保证覆盖的候选设计：稀少专业班子集作定点，其余班按校区抽 m 个。"""
    cells = eligible_cells(data)
    table = class_table(data)
    campuses = defaultdict(list)
    for class_id, meta in table.items():
        campuses[meta['campus']].append(class_id)
    rare_count = {c: len(groups[CELL_RARE]) for c, groups in cells.items()}
    rare_classes = sorted(c for c in table if rare_count[c] > 0)
    required = limits['minimum_rare_major']
    max_other = max((len(groups[CELL_OTHER]) for groups in cells.values()), default=0)
    specs = {}
    for size in range(0, len(rare_classes) + 1):
        for subset in combinations(rare_classes, size):
            if sum(rare_count[c] for c in subset) < required:
                continue
            certainty = set(subset)
            options = []
            for campus in sorted(campuses):
                pool = [c for c in sorted(campuses[campus]) if c not in certainty]
                first = 1 if pool else 0
                options.append([(campus, pool, m) for m in range(first, len(pool) + 1)])
            for combo in product(*options):
                for take in range(1, max(1, max_other) + 1):
                    strata = []
                    for campus, pool, count in combo:
                        strata.append({
                            'campus': campus,
                            'certainty': sorted(c for c in certainty
                                                if table[c]['campus'] == campus),
                            'pool': pool,
                            'sample_count': count,
                        })
                    spec = normalize_spec(data, {'strata': strata, 'other_per_class': take})
                    specs[spec_id(spec)] = spec
    return list(specs.values())


def _guaranteed_rare(spec, cells):
    return sum(len(cells[c][CELL_RARE])
               for stratum in spec['strata'] for c in stratum['certainty'])


def spec_metrics(data, spec, limits):
    cells = eligible_cells(data)
    table = class_table(data)
    take = spec['other_per_class']

    def class_cost(class_id):
        meta = table[class_id]
        interviewed = len(cells[class_id][CELL_RARE]) + \
            min(take, len(cells[class_id][CELL_OTHER]))
        return meta['visit_cost'] + meta['interview_minutes'] * interviewed

    worst = 0
    expected_size = 0.0
    for stratum in spec['strata']:
        worst += sum(class_cost(c) for c in stratum['certainty'])
        pool_costs = sorted((class_cost(c) for c in stratum['pool']), reverse=True)
        worst += sum(pool_costs[:stratum['sample_count']])
        count, size = stratum['sample_count'], len(stratum['pool'])
        for class_id in stratum['certainty']:
            expected_size += len(cells[class_id][CELL_RARE]) + \
                min(take, len(cells[class_id][CELL_OTHER]))
        for class_id in stratum['pool']:
            expected_size += (count / size) * (
                len(cells[class_id][CELL_RARE]) + min(take, len(cells[class_id][CELL_OTHER])))
    estimable, caveats = variance_estimable(spec, cells)
    return {'guaranteed_rare': _guaranteed_rare(spec, cells),
            'worst_case_minutes': worst,
            'expected_main_size': round(expected_size, 2),
            'design_variance_total': design_variance(data, spec),
            'variance_estimable': estimable,
            'caveats': caveats}


def plan(data, limits):
    """在访谈工时与稀少专业覆盖要求下搜索可行设计。

    可行则返回按设计方差排序的候选；不可同时满足时只说明冲突与可选放松，
    不生成名单、不退化为便利抽样。"""
    limits = validate_limits(limits)
    cells = eligible_cells(data)
    total_rare = sum(len(groups[CELL_RARE]) for groups in cells.values())
    evaluated = [(spec, spec_metrics(data, spec, limits)) for spec in candidate_specs(data, limits)]
    covering = [(spec, m) for spec, m in evaluated
                if m['guaranteed_rare'] >= limits['minimum_rare_major']]
    feasible = [(spec, m) for spec, m in covering
                if m['worst_case_minutes'] <= limits['minutes']]
    feasible.sort(key=lambda item: (item[1]['design_variance_total'],
                                    item[1]['worst_case_minutes']))
    response = {'operation': 'plan', 'feasible': bool(feasible), 'limits': limits,
                'frame_version': data['frame_version'], 'rare_in_frame': total_rare,
                'candidate_count': len(evaluated), 'conflicts': [], 'relaxations': []}
    if not covering:
        response['conflicts'].append({
            'constraint': 'minimum_rare_major',
            'required': limits['minimum_rare_major'],
            'available': total_rare,
            'detail': '合格抽样框中的稀少专业学生不足，任何设计都无法保证该覆盖',
        })
    cheapest_covering = min((m['worst_case_minutes'] for _, m in covering), default=None)
    if evaluated:
        cheapest_any = min(m['worst_case_minutes'] for _, m in evaluated)
    else:
        fallback = candidate_specs(data, dict(limits, minimum_rare_major=0))
        cheapest_any = min((spec_metrics(data, spec, limits)['worst_case_minutes']
                            for spec in fallback), default=None)
    if cheapest_any is not None and cheapest_any > limits['minutes']:
        response['conflicts'].append({
            'constraint': 'minutes',
            'budget': limits['minutes'],
            'required_at_least': cheapest_covering if cheapest_covering is not None else cheapest_any,
            'detail': '满足覆盖保证的最省设计也超出工时预算'
            if cheapest_covering is not None else '覆盖全部班级的最省设计也超出工时预算',
        })
    if not feasible:
        if cheapest_covering is not None:
            response['relaxations'].append({
                'kind': 'raise_budget', 'to': cheapest_covering,
                'effect': '满足稀少专业覆盖保证所需的最小访谈工时',
            })
        if limits['minimum_rare_major'] > total_rare and total_rare > 0:
            relaxed = candidate_specs(data, dict(limits, minimum_rare_major=total_rare))
            best = min(spec_metrics(data, spec, limits)['worst_case_minutes'] for spec in relaxed)
            response['relaxations'].append({
                'kind': 'lower_coverage', 'to': total_rare, 'budget_at_least': best,
                'effect': '降低到抽样框可支持的覆盖上限，并给出相应最小工时',
            })
        response['relaxations'].append({
            'kind': 'restrict_population',
            'effect': '改按部分校区或班级定义估计总体；需重新声明总体，本工具不会自动执行',
        })
        response['notice'] = '约束不可同时满足：以上为冲突与可选项，未生成任何名单，未退化为便利抽样'
        return response
    designs = []
    for spec, metrics in feasible[:MAX_DESIGNS_KEPT]:
        designs.append({
            'design_id': spec_id(spec),
            'summary': spec_summary(spec),
            'spec': spec,
            'guaranteed_rare': metrics['guaranteed_rare'],
            'worst_case_minutes': metrics['worst_case_minutes'],
            'expected_main_size': metrics['expected_main_size'],
            'design_variance_total': round(metrics['design_variance_total'], 6),
            'variance_estimable': metrics['variance_estimable'],
            'caveats': metrics['caveats'],
        })
    response['designs'] = designs
    response['kept_designs'] = len(designs)
    if not any(metrics['variance_estimable'] for _, metrics in feasible):
        estimable_costs = [m['worst_case_minutes'] for _, m in covering if m['variance_estimable']]
        response['warnings'] = [
            '预算内的可行设计都无法给出设计无偏方差：点估计可用，不确定性量化需放宽约束',
        ]
        if estimable_costs:
            response['relaxations'].append({
                'kind': 'raise_budget', 'to': min(estimable_costs),
                'effect': '使班间与层内方差成分都可设计估计所需的最小访谈工时',
            })
    response['notice'] = '候选设计均满足约束且覆盖全部校区；按精确设计方差排序，由人选定后再抽签'
    return response


# ---------- 抽签 ----------

def _chain_text(spec, stratum, class_id, cell, cells, pi):
    if class_id in stratum['certainty']:
        stage = '班级 %s 为定点必访（入班概率 1）' % class_id
    else:
        stage = '班级 %s 在校内 %d 个待抽班中随机抽 %d 个（入班概率 %d/%d）' % (
            class_id, len(stratum['pool']), stratum['sample_count'],
            stratum['sample_count'], len(stratum['pool']))
    if cell == CELL_RARE:
        within = '稀少专业层全取（层内条件概率 1）'
    else:
        others = cells[class_id][CELL_OTHER]
        take = min(spec['other_per_class'], len(others))
        within = '非稀少专业层 %d 人抽 %d 人（层内条件概率 %d/%d）' % (
            len(others), take, take, len(others))
    return '校区 %s 分层必入 → %s → %s → 入样概率 π≈%.4f' % (
        stratum['campus'], stage, within, pi)


def execute_draw(data, spec, seed):
    """按设计抽出正式名单与同层备用顺序；同一种子结果可复现。"""
    spec = normalize_spec(data, spec)
    ctx = _DesignContext(data, spec)
    table = class_table(data)
    rng = random.Random(seed)
    visited = []
    main = []
    reserves = []
    for stratum in spec['strata']:
        chosen = list(stratum['certainty'])
        if stratum['sample_count']:
            chosen += sorted(rng.sample(stratum['pool'], stratum['sample_count']))
        for class_id in sorted(chosen):
            visited.append(class_id)
            groups = ctx.cells[class_id]
            campus = table[class_id]['campus']
            for student in groups[CELL_RARE]:
                pi = ctx.pi(student)
                main.append({'student': student, 'class_id': class_id, 'campus': campus,
                             'cell': CELL_RARE, 'pi': round(pi, 6),
                             'chain': _chain_text(spec, stratum, class_id, CELL_RARE,
                                                  ctx.cells, pi)})
            others = groups[CELL_OTHER]
            take = min(spec['other_per_class'], len(others))
            picked = sorted(rng.sample(others, take)) if take else []
            rest = [s for s in others if s not in picked]
            rng.shuffle(rest)
            for student in picked:
                pi = ctx.pi(student)
                main.append({'student': student, 'class_id': class_id, 'campus': campus,
                             'cell': CELL_OTHER, 'pi': round(pi, 6),
                             'chain': _chain_text(spec, stratum, class_id, CELL_OTHER,
                                                  ctx.cells, pi)})
            reserves.append({'class_id': class_id, 'cell': CELL_OTHER, 'queue': rest})
    main.sort(key=lambda entry: entry['student'])
    return visited, main, reserves


# ---------- 现场事件与估计 ----------

def field_counts(record):
    receipts = {r['student']: r['status'] for r in record['receipts']}
    counts = {'main_size': len(record['main']), 'responded': 0, 'refused': 0,
              'absent': 0, 'out_of_frame': 0, 'pending': 0}
    for entry in record['main']:
        counts[receipts.get(entry['student'], 'pending')] += 1
    counts['substitutes_used'] = len(record['substitutions'])
    counts['substitutes_responded'] = sum(
        1 for s in record['substitutions'] if receipts.get(s['replacement']) == 'responded')
    counts['substitutes_pending'] = sum(
        1 for s in record['substitutions'] if s['replacement'] not in receipts)
    return counts


def _respondent_variance(ctx, values, respondents):
    z = {s: values[s] / ctx.pi(s) for s in respondents}
    total = 0.0
    for first in respondents:
        for second in respondents:
            pjk = ctx.pijk(first, second)
            if pjk > 0:
                total += ((pjk - ctx.pi(first) * ctx.pi(second)) / pjk) * z[first] * z[second]
    return total


def estimate(data, record):
    """沿原设计追到当前估计依据：估计总体、点估计、不确定性与现场事件链。"""
    if record['frame_version'] != data['frame_version']:
        raise ValueError('抽样框版本已变化，无法沿原设计估计该批次')
    spec = normalize_spec(data, record['design'])
    ctx = _DesignContext(data, spec)
    values = {s: row['survey_value'] for s, row in ctx.members.items()}
    receipts = {r['student']: r for r in record['receipts']}
    subs = record['substitutions']

    def status_of(student):
        receipt = receipts.get(student)
        return receipt['status'] if receipt else 'pending'

    main_ids = [entry['student'] for entry in record['main']]
    out_of_frame = sorted(s for s in main_ids if status_of(s) == 'out_of_frame')
    responded = sorted(s for s in main_ids if status_of(s) == 'responded')
    total_eligible = sum(1 for row in ctx.members.values() if row['eligibility'])
    population_size = total_eligible - len(out_of_frame)

    estimable, caveats = variance_estimable(spec, ctx.cells)
    ht_total = None
    if responded:
        ht_total = sum(values[s] / ctx.pi(s) for s in responded)
        design_based = {
            'respondents': responded,
            'ht_total': round(ht_total, 6),
            'ht_mean': round(ht_total / population_size, 6),
            'basis': '仅正式名单受访者，权重 1/π 来自原设计',
            'caution': '无响应未建模：对全总体的估计可能存在未测偏差，'
                       '该数值描述设计加权后的受访者总和',
        }
        if not estimable:
            design_based['design_variance'] = None
            design_based['variance_reason'] = '；'.join(caveats)
        elif len(responded) < 2:
            design_based['design_variance'] = None
            design_based['variance_reason'] = '受访者不足两人，无法估计设计方差'
        else:
            design_based['design_variance'] = round(
                _respondent_variance(ctx, values, responded), 6)
            design_based['variance_reason'] = None
    else:
        design_based = {
            'respondents': [],
            'ht_total': None,
            'ht_mean': None,
            'design_variance': None,
            'reason': '正式名单暂无受访者，不能构造点估计；不得以 0 或填补值冒充观测',
        }

    substitution_view = None
    if subs:
        sub_responded = [s for s in subs
                         if receipts.get(s['replacement'], {}).get('status') == 'responded']
        adjusted = (ht_total or 0.0) + sum(
            values[s['replacement']] / ctx.pi(s['original']) for s in sub_responded)
        substitution_view = {
            'substitutes_used': len(subs),
            'substitutes_responded': [s['replacement'] for s in sub_responded],
            'adjusted_total': round(adjusted, 6),
            'adjusted_mean': round(adjusted / population_size, 6),
            'assumption': '同班同专业层内被替者与替补可交换（模型假设，不是设计结论）',
            'weights_note': '替补沿用被替者的设计权重 1/π',
            'probability_basis': '含替补的最终样本不是原设计的概率样本，'
                                 '原一阶概率说明不适用于该集合',
            'design_variance': None,
            'variance_reason': '替补依赖现场响应机制，无设计无偏方差估计',
        }

    rare_achieved = sum(1 for s in responded if ctx.cell_of[s] == CELL_RARE)
    rare_achieved += sum(
        1 for s in subs
        if receipts.get(s['replacement'], {}).get('status') == 'responded'
        and ctx.cell_of[s['replacement']] == CELL_RARE)
    required = record['limits']['minimum_rare_major']
    coverage = {
        'required': required,
        'guaranteed_by_design': _guaranteed_rare(spec, ctx.cells),
        'achieved_rare_responded': rare_achieved,
        'met': rare_achieved >= required,
    }
    if not coverage['met']:
        coverage['note'] = ('稀少专业层全取、无同层备用，拒访无法递补；'
                            '实际覆盖未达要求，结论应限定或重新设计')

    events = [{'event': 'draw', 'at': record['created_at'],
               'detail': '按设计 %s 抽签（种子 %s），正式名单 %d 人'
                         % (record['design_id'], record['seed'], len(record['main']))}]
    for receipt in record['receipts']:
        events.append({'event': 'receipt', 'at': receipt['at'],
                       'detail': '%s 现场状态登记为 %s' % (receipt['student'], receipt['status'])})
    for sub in subs:
        events.append({'event': 'substitution', 'at': sub['at'],
                       'detail': '%s（%s）由同班同专业层备用 %s 递补'
                                 % (sub['original'], sub['cell'], sub['replacement'])})
    events.sort(key=lambda event: event['at'])

    return {
        'operation': 'estimate',
        'draw_id': record['draw_id'],
        'frame_version': record['frame_version'],
        'chain': {'design_id': record['design_id'],
                  'design_summary': record['design_summary'],
                  'seed': record['seed'],
                  'visited_classes': record['visited_classes'],
                  'events': events},
        'population': {
            'description': '合格抽样框（全部校区入层，未缩小总体）',
            'frame_eligible': total_eligible,
            'out_of_frame_removed': len(out_of_frame),
            'size': population_size,
            'note': ('现场确认框外 %d 人，已从估计总体剔除' % len(out_of_frame))
            if out_of_frame else '未发现框外个体',
        },
        'field': field_counts(record),
        'design_based': design_based,
        'substitution_view': substitution_view,
        'coverage_check': coverage,
    }


# ---------- 小总体枚举核对 ----------

def enumerate_samples(data, spec):
    """枚举设计的全部可能正式名单及其概率，用于核对一阶、二阶概率与设计方差。"""
    spec = normalize_spec(data, spec)
    cells = eligible_cells(data)
    per_stratum = []
    for stratum in spec['strata']:
        count, pool = stratum['sample_count'], stratum['pool']
        combos = [sorted(stratum['certainty'] + list(extra))
                  for extra in combinations(pool, count)] if count else [sorted(stratum['certainty'])]
        weight = 1.0 / len(combos)
        per_stratum.append([(combo, weight) for combo in combos])
    samples = []
    for class_choice in product(*per_stratum):
        classes = []
        p_class = 1.0
        for combo, weight in class_choice:
            classes += combo
            p_class *= weight
        per_class = []
        for class_id in sorted(classes):
            groups = cells[class_id]
            others = groups[CELL_OTHER]
            take = min(spec['other_per_class'], len(others))
            combos = [list(extra) for extra in combinations(others, take)] if take else [[]]
            weight = 1.0 / len(combos)
            per_class.append([(groups[CELL_RARE] + combo, weight) for combo in combos])
        for within in product(*per_class):
            students = []
            probability = p_class
            for group, weight in within:
                students += group
                probability *= weight
            samples.append((frozenset(students), probability))
    return samples


# ---------- 操作分发 ----------

def _limits_from(data, request):
    merged = dict(data.get('limits') or {})
    raw = request.get('limits')
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError('约束应为对象')
        merged.update(raw)
    return validate_limits(merged)


def _require_record(log, draw_id):
    if not isinstance(draw_id, str) or not draw_id:
        raise ValueError('缺少抽签批次号')
    record = log.get(draw_id)
    if record is None:
        raise ValueError('抽签记录不存在')
    return record


def _resolve_spec(data, request, limits):
    chosen = request.get('design')
    if isinstance(chosen, str):
        result = plan(data, limits)
        for item in result.get('designs', []):
            if item['design_id'] == chosen:
                return item['spec']
        raise ValueError('当前约束下不存在该可行设计编号')
    if isinstance(chosen, dict):
        inner = chosen.get('spec') if 'spec' in chosen and 'strata' not in chosen else chosen
        return normalize_spec(data, inner)
    raise ValueError('应提供设计对象或可行设计编号')


def _draw(data, request, log):
    limits = _limits_from(data, request)
    spec = _resolve_spec(data, request, limits)
    seed = request.get('seed')
    if not isinstance(seed, int):
        raise ValueError('种子应为整数')
    metrics = spec_metrics(data, spec, limits)
    problems = []
    if metrics['guaranteed_rare'] < limits['minimum_rare_major']:
        problems.append('稀少专业保证覆盖 %d 低于要求 %d'
                        % (metrics['guaranteed_rare'], limits['minimum_rare_major']))
    if metrics['worst_case_minutes'] > limits['minutes']:
        problems.append('最坏工时 %d 超出预算 %d'
                        % (metrics['worst_case_minutes'], limits['minutes']))
    if problems:
        raise ValueError('设计在当前约束下不可行：' + '；'.join(problems))
    visited, main, reserves = execute_draw(data, spec, seed)
    record = {
        'draw_id': 'dr-' + uuid.uuid4().hex[:12],
        'created_at': _now(),
        'frame_version': data['frame_version'],
        'seed': seed,
        'limits': limits,
        'design': spec,
        'design_id': spec_id(spec),
        'design_summary': spec_summary(spec),
        'visited_classes': visited,
        'main': main,
        'reserves': reserves,
        'receipts': [],
        'substitutions': [],
    }
    log.append(record)
    return {
        'operation': 'draw_v2',
        'draw_id': record['draw_id'],
        'design_id': record['design_id'],
        'design_summary': record['design_summary'],
        'seed': seed,
        'frame_version': record['frame_version'],
        'visited_classes': visited,
        'main': main,
        'reserves': reserves,
        'probability_note': '正式名单按设计概率入样；备用顺序仅在缺席或拒访时按同班同专业层启用，'
                            '启用后最终样本的概率说明以估计接口为准',
        'field': field_counts(record),
    }


def _receipt(data, request, log):
    record = _require_record(log, request.get('draw_id'))
    student = request.get('student')
    status = request.get('status')
    if status not in FIELD_STATUSES:
        raise ValueError('未知现场状态')
    known = {entry['student'] for entry in record['main']}
    known |= {s['replacement'] for s in record['substitutions']}
    if student not in known:
        raise ValueError('该学生不在此批次的正式或替补名单内')
    if any(r['student'] == student for r in record['receipts']):
        raise ValueError('该学生已有回执，不允许重复登记')
    record['receipts'].append({'student': student, 'status': status, 'at': _now()})
    log.update(record)
    return {'operation': 'receipt', 'draw_id': record['draw_id'], 'field': field_counts(record)}


def _substitute(data, request, log):
    record = _require_record(log, request.get('draw_id'))
    original = request.get('original')
    entry = next((m for m in record['main'] if m['student'] == original), None)
    if entry is None:
        raise ValueError('被替者不在正式名单内')
    receipts = {r['student']: r['status'] for r in record['receipts']}
    status = receipts.get(original)
    if status == 'out_of_frame':
        raise ValueError('框外个体应修正估计总体，不应启用替补')
    if status not in SUBSTITUTABLE:
        raise ValueError('仅缺席或拒访可启用替补')
    if any(s['original'] == original for s in record['substitutions']):
        raise ValueError('该学生已有替补记录')
    reserve = next((r for r in record['reserves']
                    if r['class_id'] == entry['class_id'] and r['cell'] == entry['cell']), None)
    if reserve is None or not reserve['queue']:
        raise ValueError('该班级该专业层无备用顺序，不得跨层递补')
    replacement = reserve['queue'].pop(0)
    substitution = {'original': original, 'replacement': replacement,
                    'class_id': entry['class_id'], 'cell': entry['cell'], 'at': _now()}
    record['substitutions'].append(substitution)
    log.update(record)
    return {
        'operation': 'substitute',
        'draw_id': record['draw_id'],
        'substitution': substitution,
        'reserve_remaining': len(reserve['queue']),
        'probability_note': '替补按事先随机顺序启用；最终样本不再等于原概率样本，'
                            '原一阶概率仅适用于正式名单，估计接口给出带明确假设的调整视图',
    }


def _summary(record):
    return {'draw_id': record['draw_id'], 'created_at': record['created_at'],
            'seed': record['seed'], 'design_id': record['design_id'],
            'design_summary': record['design_summary'],
            'visited_classes': record['visited_classes'],
            'main_size': len(record['main']),
            'receipts': len(record['receipts']),
            'substitutions': len(record['substitutions'])}


def handle(data, request, log):
    operation = request.get('operation')
    if operation == 'plan':
        return plan(data, _limits_from(data, request))
    if operation not in OPERATIONS:
        raise ValueError('未知抽签操作')
    if log is None:
        raise ValueError('该操作需要抽签日志存储')
    if operation == 'draw_v2':
        return _draw(data, request, log)
    if operation == 'receipt':
        return _receipt(data, request, log)
    if operation == 'substitute':
        return _substitute(data, request, log)
    if operation == 'estimate':
        return estimate(data, _require_record(log, request.get('draw_id')))
    if operation == 'draws':
        return {'operation': 'draws',
                'draws': [_summary(record) for record in reversed(log.list())]}
    if operation == 'draw_detail':
        return _require_record(log, request.get('draw_id'))
    raise ValueError('未知抽签操作')
