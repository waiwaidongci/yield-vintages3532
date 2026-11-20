import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from src.config.database import Base, Session, engine
from src.main import app
from src.models.identity import User
from src.models.run import Run
from src.utils.security import password_hash

POLICY = {'version': '一次通过-v1', 'numerator': 'first_pass', 'denominator': 'input', 'description': '测试口径'}
ROWS = [
    {'日期': '2026-11-01', '试制线': '试制线A', '班次': '早班', '批数': 2, '分子合计': 2322, '分母合计': 2669, '良率': 0.87},
    {'日期': '2026-11-01', '试制线': '试制线B', '班次': '早班', '批数': 2, '分子合计': 2787, '分母合计': 3178, '良率': 0.877},
    {'日期': '2026-11-01', '试制线': '试制线C', '班次': '早班', '批数': 2, '分子合计': 2470, '分母合计': 2951, '良率': 0.837},
    {'日期': '2026-11-02', '试制线': '试制线A', '班次': '晚班', '批数': 3, '分子合计': 3105, '分母合计': 3602, '良率': 0.862},
    {'日期': '2026-11-02', '试制线': '试制线B', '班次': '晚班', '批数': 3, '分子合计': 2210, '分母合计': 2911, '良率': 0.759},
]


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    pushed = []

    class FakeRedis:
        def rpush(self, queue, value):
            pushed.append((queue, value))
            return len(pushed)

    monkeypatch.setattr('src.services.hundreds._redis', lambda: FakeRedis())
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@demo.local', role=role, password=password_hash('Demo2026!')))
        snapshot = {'policy': POLICY, 'sources': [{'period': 'x'}], 'period': '全部'}
        db.add(Run(name='十一月核算', status='completed', snapshot=snapshot,
                   checksum=hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
                   result={'summary': {}, 'rows': ROWS, 'method': 'm', 'warnings': []},
                   approved=True, review='已核对数值与口径', created=time.time()))
        db.commit()
    c = TestClient(app)
    token = c.post('/auth/login', json={'email': 'analyst@demo.local', 'password': 'Demo2026!'}).json()['token']
    c.headers.update({'Authorization': 'Bearer ' + token})
    return c, pushed


def test_request_then_query_published_edition(client, monkeypatch):
    c, pushed = client
    r = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0001-abcd'})
    assert r.status_code == 200
    body = r.json()
    assert body['status'] == 'queued'
    assert pushed, '工单应进入队列'
    # worker 侧处理
    from src.services.hundreds import process
    with Session() as db:
        process(db, body['id'])
    got = c.get('/hundreds/%d' % body['id']).json()
    assert got['status'] == 'published'
    assert got['version_no'] == 1
    assert got['proved_optimal'] is True
    assert got['summary']['reason'] == ''
    listing = c.get('/hundreds').json()
    assert listing[0]['id'] == body['id']


def test_duplicate_request_key_does_not_create_second_edition(client):
    c, pushed = client
    a = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0002-abcd'}).json()
    b = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0002-abcd'}).json()
    assert a['id'] == b['id']
    assert len(pushed) == 1


def test_viewer_cannot_request(client):
    c, _ = client
    token = c.post('/auth/login', json={'email': 'viewer@demo.local', 'password': 'Demo2026!'}).json()['token']
    r = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0003-abcd'},
               headers={'Authorization': 'Bearer ' + token})
    assert r.status_code == 403


def test_csv_only_after_publish(client):
    c, _ = client
    body = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0004-abcd'}).json()
    r = c.get('/hundreds/%d/csv' % body['id'])
    assert r.status_code == 409
    assert '尚未公布' in r.json()['detail']
    from src.services.hundreds import process
    with Session() as db:
        process(db, body['id'])
    r = c.get('/hundreds/%d/csv' % body['id'])
    assert r.status_code == 200
    text = r.content.decode('utf-8-sig')
    assert '格子' in text and '厂总数' in text


def test_failure_reason_is_queryable(client, monkeypatch):
    c, _ = client
    from src.services import hundreds as svc
    from src.services.hundreds_solver import Rejected

    def boom(*args, **kwargs):
        raise Rejected('无解：不存在同时满足格值、分组小计与厂总数的百件整版', 'infeasible')

    monkeypatch.setattr(svc, 'solve', boom)
    body = c.post('/hundreds', json={'run_id': 1, 'request_key': 'ui-0005-abcd'}).json()
    with Session() as db:
        svc.process(db, body['id'])
    got = c.get('/hundreds/%d' % body['id']).json()
    assert got['status'] == 'rejected'
    assert '无解' in got['summary']['reason']
    assert c.get('/hundreds/%d/csv' % body['id']).status_code == 409
