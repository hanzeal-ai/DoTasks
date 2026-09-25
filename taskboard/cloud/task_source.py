"""Bounded read-only HTTPS feeds. Remote data never selects an execution action."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .team_directory import denied, required_text, version

_SLOTS = threading.BoundedSemaphore(8)
MAX_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 500


def source_url(value):
    value = required_text(value, '来源地址', 2000)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('来源地址无效') from None
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in {None, 443}
            or any(ord(c) < 33 or ord(c) == 127 for c in value)):
        raise ValueError('来源地址须为不含账号、密码或片段的公网 HTTPS 地址（443 端口）')
    if any(key.lower() in {'token', 'access_token', 'authorization', 'api_key', 'cursor'} for key, _ in parse_qsl(parsed.query)):
        raise ValueError('请使用完整授权地址，查询参数中不应包含令牌或分页游标')
    return urlunsplit(('https', parsed.hostname.encode('idna').decode().lower(), parsed.path or '/', parsed.query, ''))



def authorization_address(value):
    value = required_text(value, '来源授权地址', 6500)
    parsed = urlsplit(value)
    token = None
    if parsed.fragment:
        fields = parse_qsl(parsed.fragment, keep_blank_values=True)
        if len(fields) != 1 or fields[0][0] != 'token' or not fields[0][1]:
            raise ValueError('授权地址格式无效')
        token = fields[0][1]
        if len(token)>4096 or any(ord(c)<33 or ord(c)>126 for c in token):
            raise ValueError('授权地址中的令牌无效')
    return source_url(urlunsplit(parsed._replace(fragment=''))), token

def _public_address(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('无法解析来源地址') from None
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global or ipaddress.ip_address(entry[4][0]).is_multicast or ipaddress.ip_address(entry[4][0]).is_reserved for entry in addresses):
        raise ValueError('来源地址必须解析到公网，不能访问本机、内网或保留地址')
    return addresses[0][4][0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Resolve once, connect to that vetted address, retain the hostname for TLS and Host.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _get_page(url, token, remaining, deadline):
    parsed = urlsplit(url)
    address = _public_address(parsed.hostname)
    timeout = min(8, deadline - time.monotonic())
    if timeout <= 0:
        raise ValueError('拉取超时，请重试')
    connection = _PinnedHTTPS(parsed.hostname, address, timeout)
    try:
        connection.connect()
        connected_socket = connection.sock
        def abort():
            try:
                connected_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity'}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            connection.request('GET', urlunsplit(('', '', parsed.path or '/', parsed.query, '')), headers=headers)
            response = connection.getresponse()
            if response.status in {401, 403}:
                raise ValueError('来源拒绝访问，请检查授权是否有效及项目权限')
            if response.status != 200:
                raise ValueError('来源未返回成功响应；请填写最终接口地址，不支持跳转')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('来源应返回未压缩的 JSON')
            data = response.read(remaining + 1)
            if len(data) > remaining:
                raise ValueError('来源数据超过 4 MiB，请缩小查询范围')
            return data
        finally:
            timer.cancel()
    except (OSError, http.client.HTTPException):
        # Do not echo remote response bodies, URLs or credentials into logs/UI.
        raise ValueError('来源连接失败或超时，请检查地址与证书') from None
    finally:
        connection.close()


def fetch_source(url, token=''):
    if not token:
        raise ValueError('请配置有效的来源读取授权')
    url = source_url(url)
    if not _SLOTS.acquire(blocking=False):
        raise ValueError('拉取请求较多，请稍后重试')
    try:
        deadline, remaining = time.monotonic() + 30, MAX_BYTES
        items, seen_ids, cursors = [], set(), set()
        next_url = url
        for _ in range(10):
            raw = _get_page(next_url, token, remaining, deadline)
            remaining -= len(raw)
            try:
                page = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError('来源未返回有效 JSON') from None
            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                raise ValueError('来源响应需要包含 items 数组')
            for item in page['items']:
                if not isinstance(item, dict):
                    raise ValueError('来源条目必须为对象')
                row = {key: required_text(item.get(key), label, limit) for key, label, limit in (
                    ('id', '来源记录标识', 200), ('title', '来源标题', 120), ('content', '来源内容', 30000))}
                link = required_text(item.get('url'), '来源记录链接', 2000)
                parsed = urlsplit(link)
                if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or any(ord(c) < 32 for c in link):
                    raise ValueError('来源记录链接必须为 HTTP 或 HTTPS 地址且不含账号密码')
                row['url'] = link
                if row['id'] in seen_ids:
                    raise ValueError('来源响应存在重复记录，请稍后重试')
                seen_ids.add(row['id'])
                items.append(row)
                if len(items) > MAX_ITEMS:
                    raise ValueError('单次最多拉取 500 条，请缩小查询范围')
            cursor = page.get('nextCursor')
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or len(cursor) > 200 or cursor in cursors or not page['items']:
                raise ValueError('来源分页游标无效')
            cursors.add(cursor)
            parsed = urlsplit(url)
            next_url = urlunsplit(parsed._replace(query=urlencode(parse_qsl(parsed.query) + [('cursor', cursor)])))
        raise ValueError('来源超过 10 页，请缩小查询范围')
    finally:
        _SLOTS.release()


class TaskSourceMixin:
    def task_sources(self, db, allowed):
        return [dict(row) for row in db.execute('''SELECT project_id,url,coordinator_id,enabled,version,
            token!='' AS has_token FROM team_task_sources''') if row['project_id'] in allowed]

    def save_task_source(self, p):
        if self.role() != 'admin':
            denied()
        project = self.project(p.get('project_id'))
        url, token = authorization_address(p.get('url'))
        if type(p.get('enabled')) is not bool:
            raise ValueError('请明确是否启用任务来源')
        coordinator = p.get('coordinator_id')
        if self.directory.role(self.team_id, coordinator) not in {'admin','developer','coordinator'}:
            raise ValueError('请选择开发协调人')
        # Restrict the directory before writing credentials, including SQLite WAL sidecars.
        self.db.path.parent.chmod(0o700)
        self.db.path.chmod(0o600)
        with self.db.transaction() as db:
            if not db.execute('SELECT 1 FROM team_project_members WHERE project_id=? AND account_id=?', (project['id'],coordinator)).fetchone():
                denied()
            old = db.execute('SELECT * FROM team_task_sources WHERE project_id=?', (project['id'],)).fetchone()
            version(old['version'] if old else 0, p.get('version'))
            # A destination change must never forward the old destination's credential.
            secret = token if token is not None else (old['token'] if old and old['url']==url else '')
            if not secret:
                raise ValueError('请粘贴来源生成的完整授权地址，包含 #token 授权片段')
            db.execute('''INSERT INTO team_task_sources(project_id,url,coordinator_id,token,enabled,version)
                VALUES(?,?,?,?,?,1) ON CONFLICT(project_id) DO UPDATE SET url=excluded.url,
                coordinator_id=excluded.coordinator_id,token=excluded.token,enabled=excluded.enabled,version=version+1''',
                (project['id'],url,coordinator,secret,int(p['enabled'])))
        return {'ok': True}

    def source_for_pull(self, p):
        if self.role() not in {'admin','product'}:
            denied()
        project = self.project(p.get('project_id'))
        with self.db.connection() as db:
            row = db.execute('SELECT * FROM team_task_sources WHERE project_id=?', (project['id'],)).fetchone()
            if not row or not row['enabled']:
                raise ValueError('请先配置并启用任务来源')
            version(row['version'], p.get('version'))
            return dict(row)

    def import_source_items(self, source, items):
        current = self.source_for_pull(source)
        if current != source:
            raise ValueError('来源配置已变化，请重新拉取')
        created, skipped = [], 0
        with self.db.atomic() as db:
            for item in items:
                previous = db.execute('''SELECT requirement_id FROM team_source_imports
                    WHERE project_id=? AND source_url=? AND external_id=?''',
                    (source['project_id'], source['url'], item['id'])).fetchone()
                if previous:
                    skipped += 1
                    continue
                result = self.upload_requirement({'project_id':source['project_id'], 'coordinator_id':source['coordinator_id'],
                    'title':item['title'], 'content':item['content']})
                rid = result['requirement_id']
                db.execute('UPDATE requirements SET source_reference=? WHERE id=?', (item['url'],rid))
                # Pulling feedback is intake only; analysis still starts with human confirmation.
                db.execute("UPDATE team_jobs SET status='awaiting_permission' WHERE requirement_id=? AND status='queued'",(rid,))
                db.execute('INSERT INTO team_source_imports VALUES(?,?,?,?)',
                    (source['project_id'],source['url'],item['id'],rid))
                created.append(rid)
        return {'created':len(created), 'skipped':skipped, 'requirement_ids':created}
