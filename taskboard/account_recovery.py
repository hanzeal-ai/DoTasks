"""Offline operator recovery; never exposed as a public unauthenticated endpoint."""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
from pathlib import Path
import secrets
import sqlite3

from .cloud.accounts import AccountStore


def recover_account(home: Path, username: str, password: str, *, reset_device: bool = False):
    username, password = AccountStore.credentials(username, password)
    home = Path(home).expanduser().resolve()
    account_path = home / 'accounts' / 'accounts.db'
    if not account_path.is_file():
        raise ValueError('Account database does not exist; check --home')
    # Recovery requires every local executor and the cloud service to be stopped.
    # Refuse known active work rather than invalidating its callback credentials.
    for path in home.rglob('taskboard.db'):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            for table in ('task_runs', 'requirement_decomposition_runs'):
                if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
                    if db.execute(f"SELECT 1 FROM {table} WHERE status IN ('awaiting_thread','running','waiting_review') LIMIT 1").fetchone():
                        raise ValueError('Active work exists; stop and reconcile it before account recovery')
    store = AccountStore(home)
    with closing(store.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id FROM accounts WHERE username=?', (username,)).fetchone()
        if not row:
            raise ValueError('Account does not exist')
        salt = secrets.token_bytes(16)
        db.execute('UPDATE accounts SET salt=?,password_hash=? WHERE id=?',
                   (salt, store.password_hash(password, salt), row['id']))
        db.execute('DELETE FROM sessions WHERE account_id=?', (row['id'],))
        if reset_device:
            # Revoke the old bearer immediately. A password-authenticated init
            # consumes the empty device binding once; provisioned is preserved.
            db.execute('UPDATE accounts SET device_id=?,token_hash=? WHERE id=?',
                       ('', store.digest(secrets.token_urlsafe(32)), row['id']))
    return {'account_id': row['id'], 'username': username, 'device_reset': reset_device}


def main():
    parser = argparse.ArgumentParser(description='Recover a DoTasks account on the stopped cloud host')
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--reset-device', action='store_true')
    parser.add_argument('--services-stopped', action='store_true', help='Confirm cloud and local executors are stopped')
    args = parser.parse_args()
    if not args.services_stopped:
        parser.error('Stop cloud and local executors first, then pass --services-stopped')
    password = getpass.getpass('New password (12–128 characters): ')
    if password != getpass.getpass('Confirm new password: '):
        parser.error('Passwords do not match')
    try:
        result = recover_account(args.home, args.username, password, reset_device=args.reset_device)
    except ValueError as exc:
        parser.error(str(exc))
    print('Recovered account ' + result['username'] + '; browser sessions revoked.')
    if result['device_reset']:
        print('Old device credential revoked. Run dotasks init on the replacement installation with the new password.')


if __name__ == '__main__':
    main()
