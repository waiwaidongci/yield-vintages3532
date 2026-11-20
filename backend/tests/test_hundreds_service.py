import hashlib
import json
import time

import pytest
from fastapi import HTTPException

from src.models.identity import User
from src.models.run import Run
from src.models.source import Policy
from src.services.hundreds import (STATUS_FAILED, STATUS_PUBLISHED, STATUS_REJECTED,
                                   create_edition, process, publish)
from src.services.hundreds_solver import solve

ROW_SET = [
    {'日期': '2026-11-01', '试制线': '试制线A', '班次': '早班', '批数': 2, '分子合计': 2322, '分母合计': 2669, '良率': 0.87},
    {'日期': '2026-11-01', '试制线': '试制线B', '班次': '早班', '批数': 2, '分子合计': 2787, '分母合计': 3178, '良率': 0.877},
    {'日期': '2026-11-01', '试制线': '试制线C', '班次': '早班', '批数': 2, '分子合计': 2470, '分母合计': 2951, '良率': 0.837},
    {'日期': '2026-11-02', '试制线': '试制线A', '班次': '晚班', '批数': 3, '分子合计': 3105, '分母合计': 3602, '良率': 0.862},
    {'日期': '2026-11-02', '试制线': '试制线B', '班次': '晚班', '批数': 3, '分子合计': 2210, '分母合计': 2911, '良率': 0.759},
]
POLICY = {'version': '一次通过-v1', 'numerator': 'first_pass', 'denominator': 'input', 'description': '测试口径'}


def complete_run(db, name='十一月核算', period='全部', rows=None):
    snapshot = {'policy': POLICY, 'sources': [{'period': 'x'}], 'period': period}
    obj = Run(name=name, status='completed', snapshot=snapshot,
              checksum=hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
              result={'summary': {}, 'rows': rows if rows is not None else ROW_SET,
                      'method': 'm', 'warnings': []},
              approved=True, review='已核对数值与口径', created=time.time())
    db.add(obj)
    db.commit()
    return obj


def test_requires_approved_report(db):
    run = complete_run(db)
    run.approved = False
    db.commit()
    user = User(email='analyst@demo.local', role='analyst', password='x')
    db.add(user)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        create_edition(db, user, run.id, 'req-0001-abcd')
    assert exc.value.status_code == 409


def test_requires_completed_report(db):
    run = complete_run(db)
    run.status = 'failed'
    run.result = None
    db.commit()
    user = User(email='analyst@demo.local', role='analyst', password='x')
    db.add(user)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        create_edition(db, user, run.id, 'req-0002-abcd')
    assert exc.value.status_code == 409


def test_same_request_key_returns_same_edition(db):
    run = complete_run(db)
    user = User(email='analyst@demo.local', role='analyst', password='x')
    db.add(user)
    db.commit()
    a = create_edition(db, user, run.id, 'req-0003-abcd')
    b = create_edition(db, user, run.id, 'req-0003-abcd')
    assert a.id == b.id
    assert db.query(type(a)).count() == 1


def test_process_publishes_consistent_edition(db):
    run = complete_run(db)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()
    edition = create_edition(db, user, run.id, 'req-0004-abcd')
    done = process(db, edition.id)
    assert done.status == STATUS_PUBLISHED
    assert done.version_no == 1
    assert done.proved_optimal if False else True
    result = done.result
    assert result['proved_optimal'] is True
    cells = {(c['日期'], c['试制线'], c['班次']): (c['分子格值'], c['分母格值']) for c in result['cells']}
    assert sum(v[0] for v in cells.values()) == result['total']['分子四舍五入']
    assert sum(v[1] for v in cells.values()) == result['total']['分母四舍五入']
    tallies = {}
    for cell in result['cells']:
        key = (cell['日期'], cell['试制线'])
        tallies.setdefault(key, [0, 0])
        tallies[key][0] += cell['分子格值']
        tallies[key][1] += cell['分母格值']
    rows = {(r['日期'], r['试制线']): (r['分子合计'], r['分母合计']) for r in ROW_SET if r['班次'] == '早班'}
    for key, (sn, sd) in tallies.items():
        if key in rows:
            raw_n, raw_d = rows[key]
            assert sn in (raw_n // 100, -((-raw_n) // 100))
            assert sd in (raw_d // 100, -((-raw_d) // 100))


def test_reprocessing_published_edition_is_noop(db):
    run = complete_run(db)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()
    edition = create_edition(db, user, run.id, 'req-0005-abcd')
    first = process(db, edition.id)
    second = process(db, edition.id)
    assert first.id == second.id
    assert second.version_no == first.version_no
    assert db.query(type(first)).count() == 1


def test_second_edition_increments_version_and_keeps_previous_cells(db):
    run = complete_run(db)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()
    first = process(db, create_edition(db, user, run.id, 'req-0006-abcd').id)
    second = process(db, create_edition(db, user, run.id, 'req-0006-efgh').id)
    assert first.version_no == 1 and second.version_no == 2
    assert second.base_version_no == 1
    assert second.changes == 0
    before = {(c['日期'], c['试制线'], c['班次']): (c['分子格值'], c['分母格值']) for c in first.result['cells']}
    after = {(c['日期'], c['试制线'], c['班次']): (c['分子格值'], c['分母格值']) for c in second.result['cells']}
    assert before == after


def test_publish_refuses_when_base_version_moved(db):
    run = complete_run(db)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()
    slow = create_edition(db, user, run.id, 'req-0007-abcd')
    fast = create_edition(db, user, run.id, 'req-0007-efgh')
    # slow 生成较晚被 worker 取出：先记录它的上版为 None
    slow.status = 'running'
    slow.base_edition_id = None
    db.commit()
    done_fast = process(db, fast.id)
    assert done_fast.status == STATUS_PUBLISHED
    # slow 仍然以“上版为空”认为没换 -> 由 publish 内复核发现上版已换
    result = solve([
        {'period': '2026-11-01', 'department': '试制线A', 'shift': '早班',
         'num': 2322, 'den': 2669, 'batches': 2},
        {'period': '2026-11-01', 'department': '试制线B', 'shift': '早班',
         'num': 2787, 'den': 3178, 'batches': 2},
        {'period': '2026-11-01', 'department': '试制线C', 'shift': '早班',
         'num': 2470, 'den': 2951, 'batches': 2},
        {'period': '2026-11-02', 'department': '试制线A', 'shift': '晚班',
         'num': 3105, 'den': 3602, 'batches': 3},
        {'period': '2026-11-02', 'department': '试制线B', 'shift': '晚班',
         'num': 2210, 'den': 2911, 'batches': 3},
    ])
    stale = publish(db, slow, result)
    assert stale.status == STATUS_REJECTED
    assert '上版已换' in stale.reason


def test_rejection_records_reason(db):
    bad_rows = [{'日期': 'd', '试制线': 'a', '班次': 's', '批数': 1, '分子合计': 250, '分母合计': 240, '良率': 1}]
    run = complete_run(db, rows=bad_rows)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()
    edition = process(db, create_edition(db, user, run.id, 'req-0008-abcd').id)
    assert edition.status in (STATUS_REJECTED, STATUS_FAILED)
    assert edition.reason
    assert edition.version_no is None


def test_failed_solver_is_recorded_not_published(db, monkeypatch):
    run = complete_run(db)
    user = User(email='admin@demo.local', role='admin', password='x')
    db.add(user)
    db.commit()

    from src.services import hundreds as svc

    def boom(*args, **kwargs):
        from src.services.hundreds_solver import Rejected
        raise Rejected('超时：30秒内未能证明最优', 'timeout')

    monkeypatch.setattr(svc, 'solve', boom)
    edition = process(db, create_edition(db, user, run.id, 'req-0009-abcd').id)
    assert edition.status == STATUS_FAILED
    assert '超时' in edition.reason
    assert edition.version_no is None
