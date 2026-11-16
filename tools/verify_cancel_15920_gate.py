"""Read-only companion for release preparation; never replaces the 92 gate."""
from pathlib import Path
import argparse
import json
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'legacy_app'))
from cancel_notice_hold import verify, held


def check(connection, cutoff):
    result = verify(connection)
    remaining = connection.execute("SELECT event_key FROM telegram_notification_events WHERE status IN ('sending','unknown') AND julianday(created_at)>julianday(?)", (cutoff,))
    if any(not held(row['event_key']) for row in remaining):
        raise RuntimeError('CANCEL_NOTICE_UNREVIEWED_AMBIGUOUS')
    return dict(exact_cancellation=result, remaining_unreviewed=0,
                mode='INDEPENDENT_COMPANION_NOT_92_REPLACEMENT', production_installed=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',required=True)
    parser.add_argument('--policy',required=True)
    args=parser.parse_args()
    policy=json.loads(Path(args.policy).read_text())
    with sqlite3.connect(Path(args.database).resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        try:result=check(connection,policy['activated_at'])
        finally:connection.rollback()
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':main()
