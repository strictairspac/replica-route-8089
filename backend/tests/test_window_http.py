"""HTTP 端到端验证：FastAPI TestClient + SQLite，覆盖窗口/试验/取消/执行完整 API。

运行：/tmp/venv2/bin/python -I tests/test_window_http.py
"""
import os
import sys

sys.path.insert(0, '.')
os.environ['DATABASE_URL'] = 'sqlite:////tmp/window_http.db'
os.environ['REDIS_URL'] = 'redis://localhost:6379/0'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'

from fastapi.testclient import TestClient

if os.path.exists('/tmp/window_http.db'):
    os.remove('/tmp/window_http.db')

from src.config.database import Base, engine
from src.models.entities import Node, RequestSet
from src.config.database import Session
from src.main import app

Base.metadata.create_all(engine)
with Session() as db:
    db.add_all([
        Node(name='P', role='primary', status='healthy', lineage='L1', position=100,
             delay_ms=0, read_capacity=1, note=''),
        Node(name='R1', role='replica', status='healthy', lineage='L1', position=100,
             delay_ms=20, read_capacity=1, note=''),
    ])
    db.add(RequestSet(code='RS-X', title='争抢集', events=[
        {'at_ms': 10, 'kind': 'read', 'session': 's', 'hold_ms': 100},
        {'at_ms': 20, 'kind': 'read', 'session': 's', 'hold_ms': 50, 'latest_start_ms': 111},
        {'at_ms': 30, 'kind': 'read', 'session': 's', 'hold_ms': 50, 'latest_start_ms': 104},
    ], note=''))
    db.add(RequestSet(code='RS-Y', title='取消集', events=[
        {'at_ms': 15, 'kind': 'read', 'session': 's', 'hold_ms': 80},
    ], note=''))
    db.commit()

client = TestClient(app)
PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (('  -> ' + detail) if detail and not cond else ''))
    if not cond:
        raise AssertionError(name)


# 登录拿 operator token
resp = client.post('/auth/login', json={'email': 'operator@qiyuan.test', 'password': 'Demo2026!'})
# 种子关闭，用户不存在；直接造一个
from src.models.entities import User
from src.utils.security import password_hash
with Session() as db:
    if not db.get(User, 1):
        db.add(User(email='operator@qiyuan.test', password=password_hash('Demo2026!'), role='operator'))
        db.add(User(email='viewer@qiyuan.test', password=password_hash('Demo2026!'), role='viewer'))
        db.commit()
resp = client.post('/auth/login', json={'email': 'operator@qiyuan.test', 'password': 'Demo2026!'})
check('operator 登录', resp.status_code == 200, str(resp.status_code))
token = resp.json()['token']
H = {'Authorization': 'Bearer ' + token}

print('\nH1 窗口与节点容量接口')
resp = client.get('/nodes', headers=H)
check('节点返回 read_capacity', resp.status_code == 200 and resp.json()[0]['read_capacity'] == 1)
resp = client.post('/windows', headers=H, json={'name': 'HTTP共享窗口', 'horizon_ms': 5000,
                                                    'default_hold_ms': 100})
check('创建窗口 201', resp.status_code == 201, str(resp.json()))
wid = resp.json()['id']
resp = client.get('/windows', headers=H)
check('窗口列表包含新窗口', any(w['id'] == wid for w in resp.json()))

print('\nH2 发起试验与校验')
resp = client.post(f'/windows/{wid}/trials', headers=H,
                   json={'name': '试验A', 'request_set_id': 1, 'sticky_ms': 0})
check('发起试验A 201', resp.status_code == 201, str(resp.json()))
t1 = resp.json()['id']
resp = client.post(f'/windows/{wid}/trials', headers=H,
                   json={'name': '试验B', 'request_set_id': 2, 'sticky_ms': 0, 'cancel_at_ms': 50})
check('发起带取消点试验B 201', resp.status_code == 201)
t2 = resp.json()['id']
resp = client.post(f'/windows/{wid}/trials', headers=H,
                   json={'name': '越界试验', 'request_set_id': 1, 'cancel_at_ms': 99999})
check('取消点晚于时限 -> 422', resp.status_code == 422, str(resp.status_code))
resp = client.post(f'/windows/9999/trials', headers=H, json={'name': '幽灵试验', 'request_set_id': 1})
check('未知窗口 -> 404', resp.status_code == 404)

print('\nH3 取消竞争')
resp = client.post(f'/windows/{wid}/trials', headers=H,
                   json={'name': '试验C', 'request_set_id': 2, 'sticky_ms': 0})
t3 = resp.json()['id']
resp = client.post(f'/windows/{wid}/trials/{t3}/cancel', headers=H, json={'cancel_at_ms': 40})
check('排队试验取消成功', resp.status_code == 200 and resp.json()['status'] == 'cancelled')
resp = client.post(f'/windows/{wid}/trials/{t3}/cancel', headers=H, json={'cancel_at_ms': 60})
check('重复取消 -> 409', resp.status_code == 409)

print('\nH4 执行窗口与证据')
resp = client.post(f'/windows/{wid}/execute', headers=H)
check('执行窗口 200', resp.status_code == 200, str(resp.json()))
data = resp.json()
check('窗口 completed', data['status'] == 'completed')
check('结果含共享台账与 checksum', data['result']['checksum'] and len(data['result']['read_ledger']) == 5)
resp = client.post(f'/windows/{wid}/trials', headers=H, json={'name': '晚到', 'request_set_id': 1})
check('已完成窗口拒绝新试验 -> 409', resp.status_code == 409)
resp = client.post(f'/windows/{wid}/execute', headers=H)
check('重复执行 -> 409', resp.status_code == 409)
resp = client.get(f'/windows/{wid}/export', headers=H)
check('导出 JSON 200', resp.status_code == 200 and 'read_ledger' in resp.text)

print('\nH5 权限控制')
rv = client.post('/auth/login', json={'email': 'viewer@qiyuan.test', 'password': 'Demo2026!'}).json()
VH = {'Authorization': 'Bearer ' + rv['token']}
resp = client.post('/windows', headers=VH, json={'name': '观察员窗口', 'horizon_ms': 100})
check('viewer 不能创建窗口 -> 403', resp.status_code == 403)
resp = client.get(f'/windows/{wid}', headers=VH)
check('viewer 可查看窗口证据', resp.status_code == 200)

print('\nH6 失败窗口状态')
resp = client.post('/windows', headers=H, json={'name': '空窗口', 'horizon_ms': 100})
empty_id = resp.json()['id']
resp = client.post(f'/windows/{empty_id}/execute', headers=H)
check('空窗口执行 -> 422', resp.status_code == 422)
resp = client.get(f'/windows/{empty_id}', headers=H)
check('空窗口标记 failed 并保留原因', resp.json()['status'] == 'failed' and bool(resp.json()['error']))

print('\n================ 结果 ================')
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
if FAIL:
    sys.exit(1)
print('HTTP 端到端验证通过。')
