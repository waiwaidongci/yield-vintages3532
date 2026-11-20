from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response

from src.config.database import database
from src.controllers.auth import current, editor
from src.models.hundreds import HundredEdition
from src.schemas.requests import HundredInput
from src.services.hundreds import create_edition, export_rows, STATUS_PUBLISHED
from src.services.export import csv_rows
from src.utils.serialize import row

router = APIRouter()


def _public(obj):
    data = row(obj)
    result = data.pop('result', None) or {}
    data['summary'] = {
        'status': data['status'],
        'period': data['period'],
        'policy_version': data['policy_version'],
        'version_no': data['version_no'],
        'base_version_no': data['base_version_no'],
        'changes': data['changes'],
        'deviation': data['deviation'],
        'checksum': data['checksum'],
        'reason': data.get('reason') or '',
    }
    if result:
        data['total'] = result.get('total')
        data['cells'] = result.get('cells')
        data['subtotals'] = result.get('subtotals')
        data['proved_optimal'] = result.get('proved_optimal', False)
    return data


@router.get('/hundreds')
def list_editions(db=Depends(database), u=Depends(current)):
    # 列表只带摘要，避免把上百个整版的逐格明细一次性发出
    items = db.query(HundredEdition).order_by(HundredEdition.id.desc()).limit(50).all()
    out = []
    for x in items:
        data = _public(x)
        data.pop('result', None)
        data.pop('cells', None)
        data.pop('subtotals', None)
        data.pop('total', None)
        data.pop('proved_optimal', None)
        out.append(data)
    return out


@router.post('/hundreds')
def request_edition(body: HundredInput, request_key: str = Header(default='', alias='X-Request-Key'),
                    db=Depends(database), u=Depends(editor)):
    obj = create_edition(db, u, body.run_id, body.request_key or request_key)
    return _public(obj)


@router.get('/hundreds/{id}')
def detail(id: int, db=Depends(database), u=Depends(current)):
    obj = db.get(HundredEdition, id)
    if not obj:
        raise HTTPException(404, '百件版不存在')
    return _public(obj)


@router.get('/hundreds/{id}/csv')
def download(id: int, db=Depends(database), u=Depends(current)):
    obj = db.get(HundredEdition, id)
    if not obj:
        raise HTTPException(404, '百件版不存在')
    if obj.status != STATUS_PUBLISHED:
        raise HTTPException(409, '该百件版尚未公布：%s' % (obj.reason or '处理中'))
    return Response(csv_rows(export_rows(obj)), media_type='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="hundreds-%s-v%s.csv"' % (id, obj.version_no)})
