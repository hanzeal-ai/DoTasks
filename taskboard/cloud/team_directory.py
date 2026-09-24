"""Team membership lives beside account identities, never in client claims."""
from contextlib import closing
import json
import uuid

from taskboard.http_security import HTTPRequestError


def denied():
    raise HTTPRequestError(403, '没有团队或项目权限')


def required_text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} 必须为 1–{limit} 字符')
    return value.strip()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def version(actual, expected):
    if type(expected) is not int or actual != expected:
        raise HTTPRequestError(409, '版本已变化，请刷新后重新确认')


class TeamDirectory:
    def __init__(self, accounts):
        self.accounts = accounts
        with closing(accounts.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, creator_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS team_creations (actor_id TEXT NOT NULL,request_id TEXT NOT NULL,name TEXT NOT NULL,team_id TEXT NOT NULL,PRIMARY KEY(actor_id,request_id));
                CREATE TABLE IF NOT EXISTS team_members (
                    team_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','product','developer','coordinator')),
                    PRIMARY KEY(team_id, account_id));
            ''')

    def list(self, actor):
        with closing(self.accounts.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT t.*, m.role FROM teams t
                JOIN team_members m ON m.team_id=t.id WHERE m.account_id=? ORDER BY t.name''', (actor,))]

    def role(self, team, actor):
        with closing(self.accounts.connect()) as db:
            row = db.execute('SELECT role FROM team_members WHERE team_id=? AND account_id=?', (team, actor)).fetchone()
        if not row:
            denied()
        return row['role']

    def create(self, actor, name, request_id=None):
        name = required_text(name, '团队名称', 120)
        team = uuid.uuid4().hex
        with closing(self.accounts.connect()) as db, db:
            if request_id:
                request_id=required_text(request_id,'request_id',128)
                old=db.execute('SELECT * FROM team_creations WHERE actor_id=? AND request_id=?',(actor,request_id)).fetchone()
                if old:
                    if old['name']!=name: raise ValueError('请求标识已经用于其他团队名称')
                    return {'id':old['team_id'],'name':name,'role':'admin'}
                db.execute('INSERT INTO team_creations VALUES(?,?,?,?)',(actor,request_id,name,team))
            db.execute('INSERT INTO teams VALUES(?,?,?)', (team, name, actor))
            db.execute("INSERT INTO team_members VALUES(?,?,'admin')", (team, actor))
        return {'id': team, 'name': name, 'role': 'admin'}

    def members(self, team, actor):
        self.role(team, actor)
        with closing(self.accounts.connect()) as db:
            return [dict(r) for r in db.execute('''SELECT a.id, a.username, m.role
                FROM team_members m JOIN accounts a ON a.id=m.account_id
                WHERE m.team_id=? ORDER BY a.username''', (team,))]

    def add(self, team, actor, username, role):
        if self.role(team, actor) != 'admin':
            denied()
        if role not in {'product', 'developer', 'coordinator'}:
            raise ValueError('成员角色无效')
        username = required_text(username, '账号', 64).lower()
        with closing(self.accounts.connect()) as db, db:
            row = db.execute('SELECT id FROM accounts WHERE username=?', (username,)).fetchone()
            if not row:
                raise ValueError('该账号尚未注册')
            current = db.execute('SELECT role FROM team_members WHERE team_id=? AND account_id=?', (team, row['id'])).fetchone()
            if current and current['role'] == 'admin':
                raise ValueError('不能覆盖团队管理员')
            db.execute('''INSERT INTO team_members VALUES(?,?,?) ON CONFLICT(team_id,account_id)
                DO UPDATE SET role=excluded.role''', (team, row['id'], role))
        return self.members(team, actor)

    def remove(self,team,actor,account):
        if self.role(team,actor)!='admin' or account==actor:
            denied()
        with closing(self.accounts.connect()) as db, db:
            db.execute('DELETE FROM team_members WHERE team_id=? AND account_id=?',(team,account))
        return {'ok':True}
