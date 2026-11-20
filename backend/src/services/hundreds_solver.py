"""百件版整版求解器（精确整数规划，HiGHS 求解）。

业务规则（与题面一一对应）：
1. 分格：日期 × 试制线 × 班次；每格的分子、分母各有一个原数（由已复核报告的
   冻结结果汇总而来，单位是“件”）。
2. 每个格子的分子格值、分母格值，各自只能取该格原数 ÷100 的向下取整或向上
   取整。
3. 任取两个维度形成的分组小计（日期×线、日期×班次、线×班次），等于对应格值
   相加，且必须等于该组原数小计的向下取整或向上取整。
4. 厂总数等于全部格值相加，且必须等于原数总数的四舍五入（half-up）。
5. 格内分子格值不超过分母格值。
6. 目标：优先让与“同期间同口径上版”不同的格子数最少；再用“与四舍五入不
   一致的格值个数”作确定性决胜。

发布纪律：只有 HiGHS 明确返回“最优”且独立校验通过时才返回结果；无解、超时、
未能证明最优、内部校验失败一律抛 Rejected。
"""
from __future__ import annotations

import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

AXES = ('period', 'department', 'shift')
AXIS_LABELS = ('日期', '试制线', '班次')
SUBTOTAL_LABELS = (
    ('日期×试制线', (0, 1)),
    ('日期×班次', (0, 2)),
    ('试制线×班次', (1, 2)),
)
MAX_SECONDS = 30.0


class Rejected(Exception):
    def __init__(self, reason, kind='infeasible'):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind  # infeasible | timeout | unproven | invalid


def _floor100(v):
    return v // 100


def _ceil100(v):
    return -((-v) // 100)


def _half_up100(v):
    return (v + 50) // 100


def _group_bounds(values):
    """一组原数（件）取整后之和的允许区间，单位百件。"""
    total = sum(values)
    return total // 100, _ceil100(total)


def _normalize_cells(cells):
    seen = {}
    for c in cells:
        try:
            period = str(c['period'])
            department = str(c['department'])
            shift = str(c['shift'])
            num = int(c['num'])
            den = int(c['den'])
            batches = int(c.get('batches', 0))
        except (KeyError, TypeError, ValueError):
            raise Rejected('百件版输入不合法：格子字段缺失或不是整数', 'invalid')
        if not period or not department or not shift:
            raise Rejected('百件版输入不合法：日期/试制线/班次不能为空', 'invalid')
        if num < 0 or den < 0:
            raise Rejected('百件版输入不合法：分子分母原数不能为负', 'invalid')
        key = (period, department, shift)
        if key in seen:
            row = seen[key]
            row['num'] += num
            row['den'] += den
            row['batches'] += batches
        else:
            seen[key] = {'period': period, 'department': department, 'shift': shift,
                         'num': num, 'den': den, 'batches': batches}
    return [seen[k] for k in sorted(seen)]


def solve(cells, baseline=None, deadline=None):
    """求解百件整版。deadline 为 time.monotonic() 的绝对时间点。"""
    started = time.monotonic()
    if deadline is None:
        deadline = started + MAX_SECONDS - 1.5
    rows = _normalize_cells(cells)
    if not rows:
        raise Rejected('报告中没有可汇总的格子', 'invalid')
    baseline = baseline or {}
    n = len(rows)
    for r in rows:
        if _floor100(r['num']) > _ceil100(r['den']) or _ceil100(r['den']) <= 0:
            raise Rejected('无解：%s %s %s 的分子原数超过分母原数，格内分子必大于分母'
                           % (r['period'], r['department'], r['shift']), 'infeasible')

    # 变量布局：2i=分子向上指示，2i+1=分母向上指示；有上版时 2n+i=该格是否已改。
    has_base = bool(baseline)
    nv = 2 * n + (n if has_base else 0)
    lb = np.zeros(nv)
    ub = np.zeros(nv)
    for i, r in enumerate(rows):
        ub[2 * i] = 1.0 if r['num'] % 100 else 0.0
        ub[2 * i + 1] = 1.0 if r['den'] % 100 else 0.0
        if has_base:
            ub[2 * n + i] = 1.0

    row_idx = []
    col_idx = []
    data = []
    cons_lb = []
    cons_ub = []

    def add_constraint(entries, lo, hi):
        rid = len(cons_lb)
        for col, coef in entries:
            if coef:
                row_idx.append(rid)
                col_idx.append(col)
                data.append(float(coef))
        cons_lb.append(float(lo))
        cons_ub.append(float(hi))

    # 1) 三个两维分组小计
    for a, b in ((0, 1), (0, 2), (1, 2)):
        groups = {}
        for i, r in enumerate(rows):
            key = (r[AXES[a]], r[AXES[b]])
            groups.setdefault(key, []).append(i)
        for key in sorted(groups):
            idxs = groups[key]
            for comp in (0, 1):
                vals = [rows[i]['num'] if comp == 0 else rows[i]['den'] for i in idxs]
                lo, hi = _group_bounds(vals)
                floors = sum(_floor100(v) for v in vals)
                add_constraint([(2 * i + comp, 1) for i in idxs], lo - floors, hi - floors)

    # 2) 厂总数：原数四舍五入
    for comp in (0, 1):
        vals = [r['num'] if comp == 0 else r['den'] for r in rows]
        target = _half_up100(sum(vals)) - sum(_floor100(v) for v in vals)
        add_constraint([(2 * i + comp, 1) for i in range(n)], target, target)

    # 3) 格内分子不超过分母：an+un <= ad+ud  =>  un - ud <= floor(den)-floor(num)
    for i, r in enumerate(rows):
        an = _floor100(r['num'])
        ad = _floor100(r['den'])
        add_constraint([(2 * i, 1), (2 * i + 1, -1)], -np.inf, ad - an)

    # 4) 目标：字典序 —— 先最小化“与上版不同的格子数”，再最小化“与四舍五入
    #    不一致的格值个数”。用权重把两级目标压成一个整数目标。
    obj = np.zeros(nv)
    offset = 0.0
    change_weight = 2 * n + 1 if has_base else 0
    if has_base:
        for i, r in enumerate(rows):
            base_val = baseline.get((r['period'], r['department'], r['shift']))
            if base_val is None:
                continue  # 上版没有的格子不计入“改动”
            for comp in (0, 1):
                raw = r['num'] if comp == 0 else r['den']
                floor_v = _floor100(raw)
                rem = raw % 100
                base_up = base_val[comp] - floor_v
                if rem == 0:
                    const, slope = (0.0, 0.0) if base_up == 0 else (1.0, 0.0)
                elif base_up == 0:
                    const, slope = 0.0, 1.0
                elif base_up == 1:
                    const, slope = 1.0, -1.0
                else:
                    const, slope = 1.0, 0.0
                entries = [(2 * n + i, 1)]
                if slope:
                    entries.append((2 * i + comp, -slope))
                add_constraint(entries, const, np.inf)
            # 是否改动由 c_i 变量本身承担，目标里只放 weight*c_i，无需常数项。
            obj[2 * n + i] += change_weight
    for i, r in enumerate(rows):
        for comp in (0, 1):
            raw = r['num'] if comp == 0 else r['den']
            if raw % 100 == 0:
                continue
            if _half_up100(raw) - _floor100(raw) == 0:
                obj[2 * i + comp] += 1
            else:
                offset += 1
                obj[2 * i + comp] -= 1

    A = coo_matrix((data, (row_idx, col_idx)), shape=(len(cons_lb), nv)).tocsr()
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        raise Rejected('超时：30秒内未能证明最优', 'timeout')

    try:
        res = milp(
            c=obj,
            integrality=np.ones(nv),
            bounds=Bounds(lb, ub),
            constraints=LinearConstraint(A, np.array(cons_lb), np.array(cons_ub)),
            options={'time_limit': max(0.05, remaining), 'mip_rel_gap': 0.0,
                     'presolve': True, 'disp': False},
        )
    except Exception as exc:  # 求解器异常按未证最优处理
        raise Rejected('未证最优：整数规划求解失败（%s）' % exc, 'unproven')

    if res.status == 2:
        raise Rejected('无解：不存在同时满足格值、分组小计与厂总数的百件整版', 'infeasible')
    if res.status == 1:
        raise Rejected('超时：30秒内未能证明最优', 'timeout')
    if res.status != 0 or res.x is None:
        raise Rejected('未证最优：求解器未给出最优证明（%s）' % res.message, 'unproven')

    chosen = {}
    for i, r in enumerate(rows):
        un = int(round(res.x[2 * i]))
        ud = int(round(res.x[2 * i + 1]))
        un = 1 if (un == 1 and r['num'] % 100) else 0
        ud = 1 if (ud == 1 and r['den'] % 100) else 0
        chosen[i] = (_floor100(r['num']) + un, _floor100(r['den']) + ud)

    result = _materialize(rows, chosen, baseline, seconds=time.monotonic() - started)
    _verify(rows, result)
    recomputed = result['changes'] * change_weight + result['deviation']
    if abs((res.fun + offset) - recomputed) > 1e-6:
        raise Rejected('未证最优：求解目标值与整版重算值不一致', 'unproven')
    return result


def _materialize(rows, chosen, baseline, seconds):
    cells = []
    changes = 0
    deviation = 0
    for i, r in enumerate(rows):
        nv, dv = chosen[i]
        base_val = baseline.get((r['period'], r['department'], r['shift']))
        changed = base_val is not None and base_val != (nv, dv)
        dev = (0 if nv == _half_up100(r['num']) else 1) + (0 if dv == _half_up100(r['den']) else 1)
        changes += 1 if changed else 0
        deviation += dev
        cells.append({
            '日期': r['period'], '试制线': r['department'], '班次': r['shift'],
            '批数': r['batches'],
            '分子原数': r['num'], '分母原数': r['den'],
            '分子格值': nv, '分母格值': dv,
            '良率': (nv / dv) if dv else 0,
            '上版分子': base_val[0] if base_val else None,
            '上版分母': base_val[1] if base_val else None,
            '已改': '是' if changed else '否',
        })
    subtotals = {}
    for label, (a, b) in SUBTOTAL_LABELS:
        groups = {}
        for i, r in enumerate(rows):
            groups.setdefault((r[AXES[a]], r[AXES[b]]), []).append(i)
        items = []
        for key in sorted(groups):
            idxs = groups[key]
            raw_n = sum(rows[i]['num'] for i in idxs)
            raw_d = sum(rows[i]['den'] for i in idxs)
            sn = sum(chosen[i][0] for i in idxs)
            sd = sum(chosen[i][1] for i in idxs)
            items.append({
                '分组': ' / '.join(key),
                '批数': sum(rows[i]['batches'] for i in idxs),
                '分子原数': raw_n, '分母原数': raw_d,
                '分子小计': sn, '分母小计': sd,
                '小计良率': (sn / sd) if sd else 0,
            })
        subtotals[label] = items
    total_raw_n = sum(r['num'] for r in rows)
    total_raw_d = sum(r['den'] for r in rows)
    total_n = sum(v[0] for v in chosen.values())
    total_d = sum(v[1] for v in chosen.values())
    return {
        'cells': cells,
        'subtotals': subtotals,
        'total': {
            '批数': sum(r['batches'] for r in rows),
            '分子原数': total_raw_n, '分母原数': total_raw_d,
            '分子总数': total_n, '分母总数': total_d,
            '总良率': (total_n / total_d) if total_d else 0,
            '分子四舍五入': _half_up100(total_raw_n),
            '分母四舍五入': _half_up100(total_raw_d),
        },
        'changes': changes,
        'deviation': deviation,
        'proved_optimal': True,
        'seconds': round(seconds, 3),
    }


def _verify(rows, result):
    chosen = {}
    for r in result['cells']:
        nv, dv = r['分子格值'], r['分母格值']
        raw_n, raw_d = r['分子原数'], r['分母原数']
        if not (_floor100(raw_n) <= nv <= _ceil100(raw_n)):
            raise Rejected('内部校验失败：分子格值越界', 'unproven')
        if not (_floor100(raw_d) <= dv <= _ceil100(raw_d)):
            raise Rejected('内部校验失败：分母格值越界', 'unproven')
        if dv <= 0 or nv > dv:
            raise Rejected('内部校验失败：格内分子超过分母', 'unproven')
        chosen[(r['日期'], r['试制线'], r['班次'])] = (nv, dv)
    for label, (a, b) in SUBTOTAL_LABELS:
        groups = {}
        for c in rows:
            groups.setdefault((c[AXES[a]], c[AXES[b]]), []).append(c)
        submap = {tuple(item['分组'].split(' / ')): item for item in result['subtotals'][label]}
        if len(submap) != len(groups):
            raise Rejected('内部校验失败：分组小计缺失', 'unproven')
        for key, members in groups.items():
            item = submap.get(key)
            if item is None:
                raise Rejected('内部校验失败：分组小计缺失', 'unproven')
            sn = sum(chosen[(m['period'], m['department'], m['shift'])][0] for m in members)
            sd = sum(chosen[(m['period'], m['department'], m['shift'])][1] for m in members)
            if item['分子小计'] != sn or item['分母小计'] != sd:
                raise Rejected('内部校验失败：分组小计不等于格值相加', 'unproven')
            for raw_key, value, comp in (('num', sn, '分子'), ('den', sd, '分母')):
                exact = sum(m[raw_key] for m in members)
                if value not in (_floor100(exact), _ceil100(exact)):
                    raise Rejected('内部校验失败：%s小计不是原数小计的上下取整' % comp, 'unproven')
    total = result['total']
    tn = sum(v[0] for v in chosen.values())
    td = sum(v[1] for v in chosen.values())
    if total['分子总数'] != tn or total['分母总数'] != td:
        raise Rejected('内部校验失败：厂总数不等于格值相加', 'unproven')
    if tn != _half_up100(sum(r['num'] for r in rows)):
        raise Rejected('内部校验失败：分子厂总数不是原数四舍五入', 'unproven')
    if td != _half_up100(sum(r['den'] for r in rows)):
        raise Rejected('内部校验失败：分母厂总数不是原数四舍五入', 'unproven')
    if result['changes'] != sum(1 for c in result['cells'] if c['已改'] == '是'):
        raise Rejected('内部校验失败：改动格子数不一致', 'unproven')
