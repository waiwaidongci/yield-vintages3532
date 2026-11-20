import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix='yield-tests-')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(_tmp, 'test.db'))
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from src.config import database  # noqa: E402
from src.config.database import Base, engine, Session  # noqa: E402
from src.models import identity, run, source, hundreds  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        yield session


@pytest.fixture(autouse=True)
def no_redis(monkeypatch):
    """测试不依赖 Redis：队列推送改为内存记录。"""
    pushed = []

    class FakeRedis:
        def rpush(self, queue, value):
            pushed.append((queue, value))
            return len(pushed)

        def lpop(self, queue):
            for i, (q, v) in enumerate(pushed):
                if q == queue:
                    pushed.pop(i)
                    return v
            return None

    monkeypatch.setattr('src.services.hundreds._redis', lambda: FakeRedis())
    return pushed
