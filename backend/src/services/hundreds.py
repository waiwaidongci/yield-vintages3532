"""百件版业务服务：受理、生成、公布与并发判定。"""
import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.config.settings import REDIS_URL
from src.models.hundreds import HundredEdition, HundredSlot
from src.models.run import Run
from src.services.audit import log
from src.services.hundreds_solver import Rejected, solve

JOB_QUEUE = 'hundred-jobs'
STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_PUBLISHED = 'published'
STATUS_REJECTED = 'rejected'
STATUS_FAILED = 'failed'


def _redis():
    import redis
    return redis.Redis.from_url(REDIS_URL)


def create_edition(db, user, run_id, request_key):
    """受理一张百件版工单。同一 request_key 重试返回同一张单，不重复出版本。"""
    request_key = (request_key or '').strip()
    if not 8 <= len(request_key) <= 120:
        raise HTTPException(422, '请求标识不合法，请重新发起')
    existing = db.query(HundredEdition).filter_by(request_key=request_key).first()
    if existing:
        return existing
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '报告不存在')
    if run.status != 'completed' or not run.result:
        raise HTTPException(409, '百件版只能引用已完成的报告')
    if not run.approved:
        raise HTTPException(409, '百件版只能引用已复核报告')
    rows = run.result.get('rows') or []
    if not rows:
        raise HTTPException(422, '报告没有可汇总的格子')
    period = run.snapshot.get('period', '全部')
    policy_version = run.snapshot['policy']['version']
    obj = HundredEdition(
        request_key=request_key, run_id=run.id, run_checksum=run.checksum,
        period=period, policy_version=policy_version, status=STATUS_QUEUED,
        reason='', created=time.time(),
    )
    db.add(obj)
    try:
        log(db, user.email, 'hundred.queue', request_key)
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.query(HundredEdition).filter_by(request_key=request_key).one()
    _redis().rpush(JOB_QUEUE, obj.id)
    return obj


def cells_from_run(run):
    """从报告的冻结结果取格：日期×试制线×班次。"""
    cells = []
    for row in run.result.get('rows') or []:
        cells.append({
            'period': str(row['日期']), 'department': str(row['试制线']),
            'shift': str(row['班次']), 'num': int(row['分子合计']),
            'den': int(row['分母合计']), 'batches': int(row.get('批数', 0)),
        })
    return cells


def baseline_cells(edition):
    """上版（同期间同口径最近一次公布）的格值。"""
    if not edition.base_edition_id:
        return {}
    base = edition.result or {}
    out = {}
    for cell in base.get('cells') or []:
        out[(cell['日期'], cell['试制线'], cell['班次'])] = (cell['分子格值'], cell['分母格值'])
    return out


def _slot(db, period, policy_version):
    slot = db.query(HundredSlot).filter_by(period=period, policy_version=policy_version).first()
    if slot:
        return slot
    slot = HundredSlot(period=period, policy_version=policy_version, latest_edition_id=None, next_version=1)
    db.add(slot)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        slot = db.query(HundredSlot).filter_by(period=period, policy_version=policy_version).one()
    return slot


def process(db, edition_id, budget_seconds=28.0):
    """worker 调用的幂等处理：生成并尝试公布，绝不重复出版本。"""
    edition = db.get(HundredEdition, edition_id)
    if not edition or edition.status in (STATUS_PUBLISHED, STATUS_REJECTED, STATUS_FAILED):
        return edition
    run = db.get(Run, edition.run_id)
    if not run or run.status != 'completed' or not run.approved:
        return _finish_rejected(db, edition, '报告已不可用于百件版（未完成或未复核）', 'infeasible')
    edition.status = STATUS_RUNNING
    db.commit()

    try:
        _slot(db, edition.period, edition.policy_version)
        latest = (db.query(HundredEdition)
                  .filter_by(period=edition.period, policy_version=edition.policy_version,
                             status=STATUS_PUBLISHED)
                  .order_by(HundredEdition.version_no.desc()).first())
        edition.base_edition_id = latest.id if latest else None
        edition.base_version_no = latest.version_no if latest else None
        db.commit()

        deadline = time.monotonic() + budget_seconds
        result = solve(cells_from_run(run), baseline_cells(edition), deadline=deadline)
        if not result.get('proved_optimal'):
            return _finish_rejected(db, edition, '未证最优，拒绝发布', 'unproven')
        return publish(db, edition, result)
    except Rejected as exc:
        return _finish_rejected(db, edition, exc.reason, exc.kind)
    except Exception as exc:  # 任何异常都不公布
        db.rollback()
        edition = db.get(HundredEdition, edition_id)
        return _finish_rejected(db, edition, '生成失败：%s' % exc, 'unproven')


def _finish_rejected(db, edition, reason, kind):
    edition.status = STATUS_REJECTED if kind in ('infeasible', 'invalid', 'stale') else STATUS_FAILED
    edition.reason = reason
    edition.finished = time.time()
    log(db, 'worker', 'hundred.reject', '%s %s' % (edition.request_key, reason))
    db.commit()
    return edition


def publish(db, edition, result):
    """在槽位行锁内复核上版是否已换，再分配版本号并公布。"""
    try:
        # MySQL 下行锁保证同一期间×口径的公布串行；SQLite 等库忽略 FOR UPDATE
        slot = (db.query(HundredSlot)
                .filter_by(period=edition.period, policy_version=edition.policy_version)
                .with_for_update().one())
        current = db.get(HundredEdition, edition.id)
        if current.status == STATUS_PUBLISHED:
            db.commit()
            return current
        latest = (db.query(HundredEdition)
                  .filter_by(period=edition.period, policy_version=edition.policy_version,
                             status=STATUS_PUBLISHED)
                  .order_by(HundredEdition.version_no.desc()).first())
        latest_id = latest.id if latest else None
        if latest_id != current.base_edition_id:
            db.rollback()
            current = db.get(HundredEdition, edition.id)
            return _finish_rejected(db, current,
                                    '上版已换（生成期间已有新版本公布），请重新发起', 'stale')
        version_no = int(slot.next_version or 1)
        slot.next_version = version_no + 1
        slot.latest_edition_id = current.id
        current.version_no = version_no
        current.result = result
        current.checksum = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        current.changes = int(result['changes'])
        current.deviation = int(result['deviation'])
        current.status = STATUS_PUBLISHED
        current.reason = ''
        current.finished = time.time()
        log(db, 'worker', 'hundred.publish', '%s v%s' % (current.request_key, version_no))
        db.commit()
        return current
    except IntegrityError:
        db.rollback()
        current = db.get(HundredEdition, edition.id)
        return _finish_rejected(db, current, '版本号分配冲突，请重新发起', 'stale')


def export_rows(edition):
    """把整版摊平成逐格与小计行，便于 CSV 交付。"""
    result = edition.result or {}
    rows = []
    for cell in result.get('cells') or []:
        rows.append({
            '行类型': '格子', '日期': cell['日期'], '试制线': cell['试制线'], '班次': cell['班次'],
            '批数': cell['批数'], '分子原数': cell['分子原数'], '分母原数': cell['分母原数'],
            '分子格值': cell['分子格值'], '分母格值': cell['分母格值'], '良率': cell['良率'],
            '上版分子': cell['上版分子'], '上版分母': cell['上版分母'], '已改': cell['已改'],
        })
    for label, items in (result.get('subtotals') or {}).items():
        for item in items:
            rows.append({
                '行类型': '小计 %s' % label, '日期': item['分组'], '试制线': '', '班次': '',
                '批数': item['批数'], '分子原数': item['分子原数'], '分母原数': item['分母原数'],
                '分子格值': item['分子小计'], '分母格值': item['分母小计'], '良率': item['小计良率'],
            })
    total = result.get('total') or {}
    if total:
        rows.append({
            '行类型': '厂总数', '日期': '', '试制线': '', '班次': '',
            '批数': total.get('批数'), '分子原数': total.get('分子原数'), '分母原数': total.get('分母原数'),
            '分子格值': total.get('分子总数'), '分母格值': total.get('分母总数'), '良率': total.get('总良率'),
        })
    return rows
