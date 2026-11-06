"""Remove the synthetic portal database; re-initializes clean schema."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

try:
    from portal.app import init_db
except ImportError:
    from app import init_db

path = Path(os.environ.get('LINDA_DB_PATH', str(ROOT / 'portal.sqlite3')))
if path.exists():
    path.unlink()
    print(f'Reset completed: {path}')
else:
    print('No database exists; fresh state will be created.')

init_db()
print('Initialized fresh portal database schema.')
