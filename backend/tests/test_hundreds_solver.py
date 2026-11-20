import itertools
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.services.hundreds_solver import Rejected, solve


def seed_cells(seed=42, lots=240):
    rng = random.Random(seed)
    agg = defaultdict(lambda: [0, 0, 0])
    for i in range(lots):
        n = rng.randint(100, 1200)
        fp = int(n * rng.uniform(.7, .96))
        rw = rng.randint(0, n - fp)
        key = ('2026-11-%02d' % (1 + i % 20), ['试制线A', '试制线B', '试制线C'][i % 3],
               ['早班', '晚班'][i % 2])
        agg[key][0] += fp
        agg[key][1] += n
        agg[key][2] += 1
    return [{'period': k[0], 'department': k[1], 'shift': k[2],
             'num': v[0], 'den': v[1], 'batches': v[2]} for k, v in sorted(agg.items())]


def check(cells, result):
    chosen = {}
    for c in result['cells']:
        raw_n, raw_d = c['分子原数'], c['分母原数']
        nv, dv = c['分子格值'], c['分母格值']
        assert raw_n // 100 <= nv <= -((-raw_n) // 100)
        assert raw_d // 100 <= dv <= -((-raw_d) // 100)
        assert 0 < nv <= dv
        chosen[(c['日期'], c['试制线'], c['班次'])] = (nv, dv)
    for label, (a, b) in (('日期×试制线', (0, 1)), ('日期×班次', (0, 2)), ('试制线×班次', (1, 2))):
        groups = defaultdict(list)
        for c in cells:
            key = (c['period'], c['department'], c['shift'])
            groups[(key[a], key[b])].append(c)
        sub = {tuple(i['分组'].split(' / ')): i for i in result['subtotals'][label]}
        assert len(sub) == len(groups)
        for g, members in groups.items():
            sn = sum(chosen[(m['period'], m['department'], m['shift'])][0] for m in members)
            sd = sum(chosen[(m['period'], m['department'], m['shift'])][1] for m in members)
            assert sub[g]['分子小计'] == sn and sub[g]['分母小计'] == sd
            qn = sum(m['num'] for m in members)
            qd = sum(m['den'] for m in members)
            assert sn in (qn // 100, -((-qn) // 100))
            assert sd in (qd // 100, -((-qd) // 100))
    total = result['total']
    assert total['分子总数'] == sum(v[0] for v in chosen.values())
    assert total['分母总数'] == sum(v[1] for v in chosen.values())
    assert total['分子总数'] == (sum(c['num'] for c in cells) + 50) // 100
    assert total['分母总数'] == (sum(c['den'] for c in cells) + 50) // 100


def test_seed_solution_satisfies_every_rule():
    cells = seed_cells()
    result = solve(cells)
    assert result['proved_optimal'] is True
    check(cells, result)


def test_same_baseline_produces_zero_changes():
    cells = seed_cells()
    first = solve(cells)
    baseline = {(c['日期'], c['试制线'], c['班次']): (c['分子格值'], c['分母格值'])
                for c in first['cells']}
    again = solve(cells, baseline)
    assert again['changes'] == 0
    check(cells, again)


def test_matches_bruteforce_optimum_on_tiny_input():
    cells = [
        {'period': 'd1', 'department': 'a', 'shift': 's1', 'num': 250, 'den': 300, 'batches': 1},
        {'period': 'd1', 'department': 'b', 'shift': 's1', 'num': 260, 'den': 290, 'batches': 1},
        {'period': 'd1', 'department': 'a', 'shift': 's2', 'num': 740, 'den': 810, 'batches': 1},
        {'period': 'd1', 'department': 'b', 'shift': 's2', 'num': 230, 'den': 280, 'batches': 1},
    ]
    baseline = {('d1', 'a', 's1'): (3, 3), ('d1', 'b', 's1'): (2, 3),
                ('d1', 'a', 's2'): (7, 8), ('d1', 'b', 's2'): (2, 3)}
    opts = []
    for c in cells:
        an, rn = divmod(c['num'], 100)
        ad, rd = divmod(c['den'], 100)
        opts.append([(un, ud) for un in (0, 1) if not (un and rn == 0)
                     for ud in (0, 1) if not (ud and rd == 0) if an + un <= ad + ud])
    groups = defaultdict(list)
    for i, c in enumerate(cells):
        for a, b in ((0, 1), (0, 2), (1, 2)):
            key = (c['period'], c['department'], c['shift'])
            groups[(a, b, key[a], key[b])].append(i)
    best = None
    for combo in itertools.product(*[range(len(o)) for o in opts]):
        ok = True
        for (a, b, g1, g2), idxs in groups.items():
            for comp in (0, 1):
                vals = [cells[i]['num'] if comp == 0 else cells[i]['den'] for i in idxs]
                fl = sum(v // 100 for v in vals)
                tot = sum(vals)
                s = sum(opts[i][combo[i]][comp] for i in idxs)
                if not (tot // 100 - fl <= s <= -((-tot) // 100) - fl):
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue
        for comp in (0, 1):
            vals = [c['num'] if comp == 0 else c['den'] for c in cells]
            fl = sum(v // 100 for v in vals)
            k = (sum(vals) + 50) // 100 - fl
            if sum(opts[i][combo[i]][comp] for i in range(len(cells))) != k:
                ok = False
                break
        if not ok:
            continue
        chosen = {}
        for i, c in enumerate(cells):
            un, ud = opts[i][combo[i]]
            chosen[(c['period'], c['department'], c['shift'])] = (c['num'] // 100 + un, c['den'] // 100 + ud)
        cost = sum(1 for k, v in chosen.items() if baseline.get(k) != v)
        if best is None or cost < best:
            best = cost
    result = solve(cells, baseline)
    assert result['changes'] == best
    check(cells, result)


def test_stale_baseline_is_reported_not_crashed():
    cells = seed_cells()
    first = solve(cells)
    baseline = {(c['日期'], c['试制线'], c['班次']): (c['分子格值'], c['分母格值'])
                for c in first['cells']}
    key = next(iter(baseline))
    baseline[key] = (99999, 99999)
    result = solve(cells, baseline)
    check(cells, result)


def test_infeasible_input_is_rejected():
    # 一格分子原数 250 分母原数 240：向下取整后分子必大于分母
    cells = [{'period': 'd', 'department': 'a', 'shift': 's', 'num': 250, 'den': 240, 'batches': 1}]
    with pytest.raises(Rejected):
        solve(cells)


def test_timeout_is_rejected(monkeypatch):
    cells = seed_cells()
    with pytest.raises(Rejected):
        solve(cells, deadline=time.monotonic() - 1)


def test_large_input_solves_quickly():
    cells = seed_cells(seed=7, lots=1200)
    started = time.monotonic()
    result = solve(cells)
    assert time.monotonic() - started < 10
    check(cells, result)
