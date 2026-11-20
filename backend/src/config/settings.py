import os,json
from pathlib import Path
_here=Path(__file__).resolve().parents[2]
_candidates=[Path(os.getenv('CONFIG_PATH','/app/project.json')),_here/'project.json',Path('project.json')]
CONFIG=json.loads(next(p for p in _candidates if p.exists()).read_text())
DATABASE_URL=os.environ['DATABASE_URL']
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
SECRET=os.environ['JWT_SECRET']
