"""Read a running KG checkpoint without locking its replaced status.json file.

Uses SQLite read-only WAL snapshots. A stored stage does not prove that the
process is alive: separately inspect the launcher PID and stdout/stderr logs.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=Path(__file__).resolve().parent/'artifacts/kg_grounded_full_20260918_v1')
    args=parser.parse_args(); out=args.out.resolve()
    protocol=json.loads((out/'protocol.json').read_text(encoding='utf8'))
    db=sqlite3.connect((out/'state.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        db.execute('BEGIN')
        saved={key:json.loads(value) for key,value in db.execute('SELECT key,value FROM meta')}
        saved['stored_article_count']=db.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
    finally:
        db.close()
    saved.update(observed_at=datetime.now(timezone.utc).isoformat(),expected_pairs=protocol['expected_pairs'],
                 manifest_exists=(out/'manifest.json').is_file(),process_liveness_checked=False)
    print(json.dumps(saved,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':
    main()
