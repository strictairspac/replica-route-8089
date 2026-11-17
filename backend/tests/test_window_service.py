"""端到端集成验证：SQLite 内存库 + 真实 SQLAlchemy 模型与窗口服务（不经 HTTP/Redis）。

运行：/tmp/venv2/bin/python -I tests/test_window_service.py
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, '.')

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
import src.models.entities as entities
from src.models.entities import Node, RequestSet, Trial, Window
from src.services import windows as svc
from src.schemas.window import WindowInput, WindowTrialInput, CancelInput

engine = create_engine('sqlite:///:memory:')
Base.metadata.create_all(engine)
Session = sessionmaker(engine, expire_on_commit=False)
user = SimpleNamespace(email='operator@qiyuan.test')

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (('  -> ' + detail) if detail and not cond else ''))
    if not cond:
        raise AssertionError(name)


with Session() as db:
    # 单主库单槽拓扑：所有读争抢同一份容量，排队/期限/取消语义唯一可判定
    db.add_all([
        Node(name='P', role='primary', status='healthy', lineage='L1', position=100,
             delay_ms=0, read_capacity=1, note=''),
    ])
    db.add(RequestSet(code='RS-A', title='争抢请求集A', events=[
        {'at_ms': 10, 'kind': 'read', 'session': 's', 'hold_ms': 100},
        {'at_ms': 20, 'kind': 'read', 'session': 's', 'hold_ms': 50, 'latest_start_ms': 111},
        {'at_ms': 30, 'kind': 'read', 'session': 's', 'hold_ms': 50, 'latest_start_ms': 104},
    ], note=''))
    db.add(RequestSet(code='RS-B', title='取消请求集B', events=[
        {'at_ms': 15, 'kind': 'read', 'session': 's', 'hold_ms': 80},
    ], note=''))
    db.commit()

    print('\nS1 窗口配置与试验发起')
    w = svc.create(db, WindowInput(name='交接班窗口', default_hold_ms=100, horizon_ms=5000), user)
    check('窗口初始 open', w['status'] == 'open')
    t1 = svc.add_trial(db, w['id'], WindowTrialInput(name='试验A', request_set_id=1,
                                                     max_delay_ms=100, sticky_ms=0), user)
    t2 = svc.add_trial(db, w['id'], WindowTrialInput(name='试验B', request_set_id=2,
                                                     max_delay_ms=100, sticky_ms=0,
                                                     cancel_at_ms=50), user)
    check('两个试验均入窗且 queued', t1['status'] == t2['status'] == 'queued' and t1['window_id'] == w['id'])
    check('试验节点快照包含 read_capacity', t1['input_snapshot']['nodes'][0]['read_capacity'] == 1)

    print('\nS2 取消竞争（排队中撤回）')
    # t2 自带 cancel_at_ms=50；再验证显式取消接口对排队试验生效
    t3 = svc.add_trial(db, w['id'], WindowTrialInput(name='试验C', request_set_id=2,
                                                     max_delay_ms=100, sticky_ms=0), user)
    cancelled = svc.cancel_trial(db, w['id'], t3['id'], CancelInput(cancel_at_ms=40), user)
    check('取消后状态 cancelled 且记录取消时刻', cancelled['status'] == 'cancelled'
          and cancelled['cancel_at_ms'] == 40)
    try:
        svc.cancel_trial(db, w['id'], t3['id'], CancelInput(cancel_at_ms=60), user)
        check('已取消试验不能重复取消', False)
    except Exception as e:
        check('已取消试验拒绝重复取消（409）', getattr(e, 'status_code', None) == 409)

    print('\nS3 执行窗口并核对共享时间轴证据')
    detail = svc.execute(db, w['id'], user)
    result = detail['result']
    check('窗口完成', detail['status'] == 'completed' and result is not None)
    check('窗口内 3 个试验（含已取消C）', result['summary']['trials'] == 3)
    # t2 自带时间轴取消点（50）、t3 经取消接口显式取消（40），引擎视角两者都 cancelled
    check('两个带取消点的试验都计入 cancelled_trials',
          result['summary']['cancelled_trials'] == 2, str(result['summary']))
    ledger = result['read_ledger']
    # 单主库单槽：A-1@10 占 P[10,110)；B@15、C@15 排队后分别在 50/40 撤回，不占容量
    a_rows = [e for e in ledger if e['trial_id'] == t1['id']]
    check('A-1 立即启动', a_rows[0]['start_ms'] == 10 and a_rows[0]['node'] == 'P')
    check('A-2 释放点110早于期限111，释放即启动（期限内）',
          a_rows[1]['status'] == 'scheduled' and a_rows[1]['start_ms'] == 110, str(a_rows[1]))
    check('A-3 期限104 严格早于释放点 -> 过期拒绝', a_rows[2]['status'] == 'expired', str(a_rows[2]))
    b_rows = [e for e in ledger if e['trial_id'] == t2['id']]
    check('B-1 在时间轴取消点50被撤回，不占容量', b_rows[0]['status'] == 'withdrawn'
          and b_rows[0]['node'] is None, str(b_rows[0]))
    check('B 仅一个读，取消后无后续台账', len(b_rows) == 1)
    c_rows = [e for e in ledger if e['trial_id'] == t3['id']]
    check('C 单读在显式取消点40撤回', len(c_rows) == 1 and c_rows[0]['status'] == 'withdrawn')
    # 容量不变量
    intervals = [(e['start_ms'], e['end_ms'], e['node']) for e in ledger if e['status'] == 'scheduled']
    ok = all(sum(1 for s2, e2, n2 in intervals if n2 == n and s2 < e and s < e2) <= 1
             for s, e, n in intervals)
    check('任意时刻主库并发不超过容量1', ok)
    check('全部已调度读安全', result['summary']['all_scheduled_reads_safe'] is True)
    check('结果带确定性 checksum', len(result['checksum']) == 64)

    print('\nS4 试验级证据与状态落库')
    t1_db = db.get(Trial, t1['id'])
    check('试验回写窗口级分片结果', t1_db.result is not None and 'read_ledger' in t1_db.result)
    check('未取消试验 completed，显式取消试验保持 cancelled',
          t1_db.status == 'completed' and db.get(Trial, t2['id']).status == 'completed'
          and db.get(Trial, t3['id']).status == 'cancelled')
    per = {t['trial_id']: t['status'] for t in result['trial_results']}
    check('引擎分片：t2/t3 均为 cancelled，t1 completed',
          per[t1['id']] == 'completed' and per[t2['id']] == 'cancelled'
          and per[t3['id']] == 'cancelled')

    print('\nS5 校验失败不落完成态 / 已关闭窗口拒绝新试验')
    w2 = svc.create(db, WindowInput(name='空窗口', horizon_ms=100), user)
    try:
        svc.execute(db, w2['id'], user)
        check('空窗口执行被拒绝', False)
    except Exception as e:
        check('空窗口执行返回 422', getattr(e, 'status_code', None) == 422)
    db.expire_all()  # 失败标记已在服务内提交，刷新会话视图即可
    check('失败窗口标记 failed 并记录原因', db.get(Window, w2['id']).status == 'failed'
          and db.get(Window, w2['id']).error)
    try:
        svc.add_trial(db, w['id'], WindowTrialInput(name='晚到试验Y', request_set_id=1), user)
        check('已完成窗口拒绝新试验', False)
    except Exception as e:
        check('已完成窗口拒绝新试验（409）', getattr(e, 'status_code', None) == 409)
    db.expire_all()

print('\n================ 结果 ================')
print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
if FAIL:
    sys.exit(1)
print('服务层端到端验证通过。')
