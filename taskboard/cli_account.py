"""Display this installation's account; keep recoverable passwords in Keychain."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
import json
import subprocess
import sys
from urllib.parse import urlparse

from .agent import load_agent_config
from .runtime_paths import default_data_home


class PasswordStore:
    def __init__(self):
        if sys.platform != 'darwin':
            raise RuntimeError('账户密码存储需要 macOS 钥匙串。')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        signatures = {
            'CFStringCreateWithCString': (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_uint32]),
            'CFDataCreate': (C.c_void_p, [C.c_void_p, C.c_char_p, C.c_long]),
            'CFDataGetLength': (C.c_long, [C.c_void_p]),
            'CFDataGetBytePtr': (C.c_void_p, [C.c_void_p]),
            'CFDictionaryCreateMutable': (C.c_void_p, [C.c_void_p, C.c_long, C.c_void_p, C.c_void_p]),
            'CFDictionarySetValue': (None, [C.c_void_p, C.c_void_p, C.c_void_p]),
            'CFRelease': (None, [C.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(self.cf, name)
            function.restype, function.argtypes = result, arguments
        for name in ('SecItemCopyMatching', 'SecItemAdd'):
            function = getattr(self.sec, name)
            function.restype, function.argtypes = C.c_int32, [C.c_void_p, C.POINTER(C.c_void_p)]
        self.sec.SecItemUpdate.restype = C.c_int32
        self.sec.SecItemUpdate.argtypes = [C.c_void_p, C.c_void_p]

    def constant(self, name):
        library = self.cf if name.startswith('kCF') else self.sec
        return C.c_void_p.in_dll(library, name).value

    @contextmanager
    def dictionary(self, values):
        query = self.cf.CFDictionaryCreateMutable(None, 0,
            C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks')),
            C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks')))
        try:
            for key, value in values.items():
                owned = isinstance(value, (str, bytes))
                if isinstance(value, str):
                    pointer = self.cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
                elif isinstance(value, bytes):
                    pointer = self.cf.CFDataCreate(None, value, len(value))
                else:
                    pointer = value
                self.cf.CFDictionarySetValue(query, self.constant(key), pointer)
                if owned:
                    self.cf.CFRelease(pointer)
            yield query
        finally:
            self.cf.CFRelease(query)

    def identity(self, cloud, username, agent_id):
        if any(not isinstance(value, str) or not value or '\0' in value for value in (cloud, username, agent_id)):
            raise ValueError('无效的钥匙串账户标识。')
        return {'kSecClass': self.constant('kSecClassGenericPassword'),
                'kSecAttrService': f'com.dotasks.account:{cloud}:{agent_id}',
                'kSecAttrAccount': username}

    def save(self, cloud, username, agent_id, password):
        identity = self.identity(cloud, username, agent_id)
        with self.dictionary(identity) as query, self.dictionary({'kSecValueData': password.encode()}) as value:
            status = self.sec.SecItemUpdate(query, value)
        if status == -25300:  # errSecItemNotFound
            with self.dictionary({**identity, 'kSecValueData': password.encode()}) as query:
                status = self.sec.SecItemAdd(query, None)
        if status:
            raise RuntimeError(f'无法保存账户密码到 macOS 钥匙串（{status}）。')

    def read(self, cloud, username, agent_id):
        identity = self.identity(cloud, username, agent_id)
        result = C.c_void_p()
        with self.dictionary({**identity, 'kSecReturnData': self.constant('kCFBooleanTrue'),
                              'kSecMatchLimit': self.constant('kSecMatchLimitOne')}) as query:
            status = self.sec.SecItemCopyMatching(query, C.byref(result))
        if status == -25300:
            return None
        if status:
            raise RuntimeError(f'无法读取 macOS 钥匙串中的账户密码（{status}）。')
        try:
            return C.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result)).decode()
        finally:
            self.cf.CFRelease(result)


def show_account(*, password=None):
    receipt_path = default_data_home() / 'onboarding.json'
    if not receipt_path.is_file():
        raise RuntimeError('本机没有注册账号信息；旧 Token 配置无法推导用户名，请先完成 dotasks init。')
    receipt = json.loads(receipt_path.read_text())
    config = load_agent_config()
    if (receipt.get('agent_id') != config.agent_id or receipt.get('cloud_url') != config.cloud_url
            or receipt.get('device_token') != config.agent_token or not isinstance(receipt.get('username'), str)
            or not receipt['username'] or '\0' in receipt['username']):
        raise RuntimeError('账号记录与当前设备绑定不一致，无法显示账户信息。')
    print(f'账号：{receipt["username"]}')
    if password is None:
        try:
            password = PasswordStore().read(config.cloud_url, receipt['username'], config.agent_id)
        except (OSError, RuntimeError) as exc:
            print(f'密码：暂时无法读取；{exc}')
            print(f'云端：{config.cloud_url}')
            raise
    print(f'密码：{password}' if password is not None else '密码：本机未保存，无法取回原密码。')
    print(f'云端：{config.cloud_url}')
    print(f'设备：{config.agent_id}')


def open_cloud(cloud):
    parsed = urlparse(cloud)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('无法打开无效的云端地址。')
    try:
        subprocess.run(['/usr/bin/open', cloud], check=True, capture_output=True, timeout=15)
        print('已在浏览器打开云端。')
    except (OSError, subprocess.SubprocessError):
        print(f'浏览器未能自动打开，请手动访问 {cloud}')
