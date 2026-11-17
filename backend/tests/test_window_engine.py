"""共享演练窗口引擎验证：纯标准库，python3 -I tests/test_window_engine.py 直接运行。

覆盖：
T1  跨试验容量争抢与 FIFO 排队、释放后按序启动、不抢占剩余容量
T2  期限边界：latest_start 恰好等于可启动时刻（闭区间）vs 严格超出（过期）
T3  取消竞争：排队中撤回不占容量、已开始保留至结束、已完成不改写、取消后事件跳过
T4  等待期间复制推进：故障期间排队，复制达标后重新选路（对照无复制则持续等待/关闭）
T5  等待期间节点故障：占用保留、等待读改道、down 节点不再接新读、恢复后回流
T6  写后读粘滞 + 容量：粘滞期只选主库，容量不足排队，释放后仍在粘滞窗口则走主库
T7  重试不重复占用：同一入参重复重放，台账与校验和完全一致
T8  窗口时限：期限内最后一刻可启动，越线排队读关闭拒绝
T9  每节点容量独立：一个节点打满不影响另一节点；主库回退容量
T10 等待期间主库故障：回退主库不可行，恢复后重新调度
T11 结构校验与确定性：非法输入拒绝、结果可复现、会话隔离
"""
import copy
import sys

sys.path.insert(0, '.')
from src.services.window_engine import run, validate_input  # noqa: E402

PASSED = []
FAILED = []


def check(name, cond, detail=''):
    (PASSED if cond else FAILED).append(name)
    print(('  ✓ ' if cond else '  ✗ ') + name + (('  -> ' + detail) if detail and not cond else ''))
    if not cond:
        raise AssertionError(name + ' ' + detail)


def base_nodes(p_cap=10, r1_cap=1, r2_cap=10):
    return [
        {'name': 'P', 'role': 'primary', 'status': 'healthy', 'lineage': 'L1',
         'position': 100, 'delay_ms': 0, 'read_capacity': p_cap},
        {'name': 'R1', 'role': 'replica', 'status': 'healthy', 'lineage': 'L1',
         'position': 100, 'delay_ms': 20, 'read_capacity': r1_cap},
        {'name': 'R2', 'role': 'replica', 'status': 'healthy', 'lineage': 'L1',
         'position': 90, 'delay_ms': 40, 'read_capacity': r2_cap},
    ]


def trial(tid, name, events, cancel=None, max_delay=100, sticky=0):
    t = {'id': tid, 'name': name, 'max_delay_ms': max_delay, 'sticky_ms': sticky,
         'events': events}
    if cancel is not None:
        t['cancel_at_ms'] = cancel
    return t


def read(at, session='s', hold=None, latest=None):
    e = {'at_ms': at, 'kind': 'read', 'session': session}
    if hold is not None:
        e['hold_ms'] = hold
    if latest is not None:
        e['latest_start_ms'] = latest
    return e


def write(at, session='s'):
    return {'at_ms': at, 'kind': 'write', 'session': session}


def window(hold=100, horizon=5000, wid='w1', name='共享窗口'):
    return {'id': wid, 'name': name, 'default_hold_ms': hold, 'horizon_ms': horizon}


def ledger_of(result, tid):
    return next(t for t in result['trial_results'] if t['trial_id'] == tid)['read_ledger']


def entries_at_node(result, node):
    return [e for e in result['read_ledger'] if e['status'] == 'scheduled' and e['node'] == node]


def assert_capacity_invariant(result, nodes):
    """任何节点任意时刻的在途读数量不得超过其 read_capacity（含跨试验）。"""
    caps = {n['name']: n['read_capacity'] for n in nodes}
    intervals = [(e['start_ms'], e['end_ms'], e['node'])
                 for e in result['read_ledger'] if e['status'] == 'scheduled']
    for s, e, node in intervals:
        overlapping = sum(1 for (s2, e2, n2) in intervals if n2 == node and s2 < e and s < e2)
        check('容量不变量：节点 %s 在 %sms 并发 %d<=%d' % (node, s, overlapping, caps[node]),
              overlapping <= caps[node])
    for e in result['read_ledger']:
        if e['status'] == 'scheduled':
            check('占用时长=hold：seq%s' % e['seq'], e['end_ms'] - e['start_ms'] == e['hold_ms'])
            check('启动不早于到达：seq%s' % e['seq'], e['start_ms'] >= e['at_ms'])
            check('等待时长口径：seq%s' % e['seq'], e['wait_ms'] == e['start_ms'] - e['at_ms'])


print('\nT1 跨试验争抢 / FIFO 排队 / 释放后按序启动')
# 单主库单槽：holder 占 P[5,105)；A、B 两个试验的读在容量全满期间到达，
# 进入跨试验共享队列；105 释放后严格按到达先后启动，互不抢占。
single = [{'name': 'P', 'role': 'primary', 'status': 'healthy', 'lineage': 'L1',
           'position': 100, 'delay_ms': 0, 'read_capacity': 1}]
nodes = single
holder = trial('H', '占座试验', [read(5, hold=100)])
trials = [
    holder,
    trial('A', '试验A', [read(10, hold=50)]),
    trial('B', '试验B', [read(20, hold=50)]),
]
res = run(nodes, trials, window())
la, lb = ledger_of(res, 'A'), ledger_of(res, 'B')
check('A 排队至 105 释放后启动', la[0]['status'] == 'scheduled' and la[0]['start_ms'] == 105
      and la[0]['wait_ms'] == 95, str(la[0]))
check('B 排在 A 之后，155 启动（FIFO 不抢占）', lb[0]['status'] == 'scheduled' and lb[0]['start_ms'] == 155
      and lb[0]['wait_ms'] == 135, str(lb[0]))
check('峰值排队深度=2（跨试验共享队列）', res['summary']['peak_queue_depth'] == 2)
check('峰值在途=1（单槽）', res['summary']['peak_in_flight'] == 1)
assert_capacity_invariant(res, nodes)

print('\nT2 期限边界（闭区间 <= 可启动，> 过期）')
nodes = single
holder = trial('H', '占座试验', [read(5, hold=100)])  # P 占满至 105
trials = [
    holder,
    trial('B', '试验B', [read(20, hold=10, latest=105)]),   # 期限恰为释放点
    trial('C', '试验C', [read(30, hold=10, latest=104)]),   # 期限严格早于释放点
]
res = run(nodes, trials, window())
lb, lc = ledger_of(res, 'B'), ledger_of(res, 'C')
check('B 期限=105 恰在释放点启动（边界内）', lb[0]['status'] == 'scheduled' and lb[0]['start_ms'] == 105, str(lb[0]))
check('C 期限=104 严格超出则过期拒绝', lc[0]['status'] == 'expired' and lc[0]['node'] is None, str(lc[0]))
check('C 的 trace 决策为 rejected', any(r['kind'] == 'read' and r['decision'] == 'rejected'
                                         and r['trial_id'] == 'C' for r in res['trace']))
assert_capacity_invariant(res, nodes)

print('\nT3 取消竞争')
nodes = single
# D 读[2,5) 先完成；C 读@5 占 P[5,105)，60 取消 C —— 已开始占用保留至 105
# holder 读@20 排队；B 读@30 排队，50 取消 B —— 撤回且不占容量，80 的后续读不再入队
trials = [
    trial('D', '已完成试验', [read(2, hold=3)]),
    trial('C', '在途取消试验', [read(5, hold=100)], cancel=60),
    trial('H', '占座试验', [read(20, hold=200)]),
    trial('B', '排队取消试验', [read(30, hold=50), read(80)], cancel=50),
]
res = run(nodes, trials, window())
lb, lc, lh, ld = (ledger_of(res, 'B'), ledger_of(res, 'C'),
                  ledger_of(res, 'H'), ledger_of(res, 'D'))
check('B 排队中的读被撤回且不占容量', lb[0]['status'] == 'withdrawn' and lb[0]['node'] is None, str(lb[0]))
check('B 取消后的读(80)不再产生台账', len(lb) == 1)
check('C 已开始的读保留至自然结束 105', lc[0]['status'] == 'scheduled' and lc[0]['start_ms'] == 5
      and lc[0]['end_ms'] == 105, str(lc[0]))
check('C 取消后释放的槽位被排队中的 holder 按序接上（105 启动）',
      lh[0]['status'] == 'scheduled' and lh[0]['start_ms'] == 105, str(lh[0]))
check('D 已完成决策不受任何取消改写', ld[0]['status'] == 'scheduled' and ld[0]['end_ms'] == 5)
check('B/C 试验状态 cancelled，其余 completed',
      {t['trial_id']: t['status'] for t in res['trial_results']} ==
      {'D': 'completed', 'C': 'cancelled', 'H': 'completed', 'B': 'cancelled'})
check('取消事件写入共享时间轴', sum(r['kind'] == 'trial_cancel' for r in res['trace']) == 2)
assert_capacity_invariant(res, nodes)

print('\nT4 等待期间复制推进影响选路')
# R1 全程被 holder 占用；B 先写使 required=101，R2 初始位点 90 不达标；P 在 5~90 下线
nodes = base_nodes(p_cap=10, r1_cap=1, r2_cap=10)
holder = trial('H', '占座试验', [read(2, hold=400)])  # R1[2,402)
common_topo = [{'at_ms': 5, 'kind': 'node_down', 'node': 'P'},
               {'at_ms': 90, 'kind': 'node_up', 'node': 'P'}]
# 场景一：50ms R2 复制到位点101 -> B 在 50 即被调度到 R2（复制事件插在故障与恢复之间）
trials_repl = [
    holder,
    trial('B', '试验B', [write(0), common_topo[0], read(10, hold=50),
                         {'at_ms': 50, 'kind': 'replicate', 'node': 'R2',
                          'position': 101, 'delay_ms': 40}, common_topo[1]]),
]
res = run(nodes, trials_repl, window())
lb = ledger_of(res, 'B')
check('B 写后位点需求=101，R2 初始不达标而排队', lb[0]['required_position'] == 101)
check('复制达标后 B 立即改走 R2（无需等主库恢复）', lb[0]['node'] == 'R2'
      and lb[0]['start_ms'] == 50 and lb[0]['wait_ms'] == 40, str(lb[0]))
# 场景二对照：无复制，R2 永远不达标 -> B 等到 90 主库恢复后回退 P
trials_ctrl = [holder, trial('B', '试验B', [write(0), common_topo[0], read(10, hold=50), common_topo[1]])]
res_ctrl = run(nodes, trials_ctrl, window())
lb_ctrl = ledger_of(res_ctrl, 'B')
check('无复制时 B 持续排队至主库恢复，回退 P', lb_ctrl[0]['node'] == 'P'
      and lb_ctrl[0]['start_ms'] == 90 and lb_ctrl[0]['wait_ms'] == 80, str(lb_ctrl[0]))
assert_capacity_invariant(res, nodes)
assert_capacity_invariant(res_ctrl, nodes)

print('\nT5 等待期间节点故障 / 恢复改道')
# holder 占 R1[5,205) 与 R2[6,150)；同一试验先写使 required=101，R2 位点90 在 80 复制前不可选
# B读@20：R1满/R2不达标 -> 回退 P[20,70)；40 P 下线不影响已开始占用
# C读@60：P down、R1 满、R2 不达标 -> 排队；80 R2 复制到位点101 -> C 改走 R2；120 P 恢复
nodes = base_nodes(p_cap=10, r1_cap=1, r2_cap=1)
holder = trial('H', '占座试验', [read(5, hold=200), read(6, hold=64)])  # R1[5,205) R2[6,70)
trials = [
    holder,
    trial('T', '故障改道试验', [write(0), read(20, hold=50),
                               {'at_ms': 40, 'kind': 'node_down', 'node': 'P'},
                               read(60, hold=30),
                               {'at_ms': 80, 'kind': 'replicate', 'node': 'R2',
                                'position': 101, 'delay_ms': 40},
                               {'at_ms': 120, 'kind': 'node_up', 'node': 'P'}]),
]
res = run(nodes, trials, window())
lt = ledger_of(res, 'T')
check('B 回退主库且已开始占用不受 P 故障影响，保留至 70',
      lt[0]['node'] == 'P' and lt[0]['end_ms'] == 70, str(lt[0]))
check('C 故障期间排队，R2 复制达标后于 80 改道 R2', lt[1]['node'] == 'R2'
      and lt[1]['start_ms'] == 80 and lt[1]['wait_ms'] == 20, str(lt[1]))
check('down 的 P 在 40~120 不承接任何新读', all(not (e['node'] == 'P' and 40 <= e['start_ms'] < 120)
                                               for e in res['read_ledger'] if e['status'] == 'scheduled'))
# 恢复后回流对照：无复制事件，R2 始终不达标 -> C 等 P 恢复（120）再走主库
holder2 = trial('H', '占座试验', [read(5, hold=300), read(6, hold=300)])
trials_ctrl = [
    holder2,
    trial('T', '故障恢复试验', [write(0), read(20, hold=50),
                               {'at_ms': 40, 'kind': 'node_down', 'node': 'P'},
                               read(60, hold=30),
                               {'at_ms': 120, 'kind': 'node_up', 'node': 'P'}]),
]
res_ctrl = run(nodes, trials_ctrl, window())
lt_ctrl = ledger_of(res_ctrl, 'T')
check('主库恢复后 C 回流 P（120 启动）', lt_ctrl[1]['node'] == 'P' and lt_ctrl[1]['start_ms'] == 120, str(lt_ctrl[1]))
assert_capacity_invariant(res, nodes)
assert_capacity_invariant(res_ctrl, nodes)

print('\nT6 写后读粘滞 + 容量')
# 单主库单槽 + 粘滞：H 读@5 占 P[5,110)；A t=0 写、t=10 粘滞读只能走主库 -> 排队；
# B t=20 到达，同样落在 sticky=500 窗口内且主库满 -> 排队；110 释放后 A、B 按 FIFO 仍走主库。
nodes = single
trials = [
    trial('H', '占座试验', [read(5, hold=105)], sticky=500),
    trial('A', '写后读试验', [write(0), read(10, hold=100)], sticky=500),
    trial('B', '粘滞试验', [read(20, hold=20)], sticky=500),
]
res = run(nodes, trials, window())
la, lb = ledger_of(res, 'A'), ledger_of(res, 'B')
check('粘滞读 A 排队后于 110 走主库', la[0]['node'] == 'P' and la[0]['start_ms'] == 110 and la[0]['end_ms'] == 210, str(la[0]))
check('B 粘滞期主库满则排队，A 释放后 210 仍走主库', lb[0]['node'] == 'P' and lb[0]['start_ms'] == 210
      and lb[0]['wait_ms'] == 190, str(lb[0]))
check('粘滞读没有落到任何副本', all(e['node'] == 'P' for e in res['read_ledger'] if e['status'] == 'scheduled'))
check('所有已路由读满足写后读', res['summary']['all_scheduled_reads_safe'] is True)
assert_capacity_invariant(res, nodes)

print('\nT7 重试不重复占用（确定性重放）')
nodes = base_nodes()
trials = [trial('A', '试验A', [write(0), read(10, hold=30, latest=200), read(20, hold=20)]),
          trial('B', '试验B', [read(15, hold=40), read(60, hold=10)])]
w = window()
r1 = run(copy.deepcopy(nodes), copy.deepcopy(trials), w)
r2 = run(copy.deepcopy(nodes), copy.deepcopy(trials), w)
check('两次重放校验和一致', r1['checksum'] == r2['checksum'])
check('台账条目数一致且不翻倍', len(r1['read_ledger']) == len(r2['read_ledger']) == 4)
check('调度去向逐字节一致', [(e['seq'], e['status'], e['node'], e['start_ms'], e['end_ms'])
                            for e in r1['read_ledger']]
      == [(e['seq'], e['status'], e['node'], e['start_ms'], e['end_ms']) for e in r2['read_ledger']])

print('\nT8 窗口时限边界')
nodes = single
w = window(horizon=200)
# holder 占 P[5,210)：释放点严格晚于 horizon，窗口内不会空出槽位
holder = trial('H', '占座试验', [read(5, hold=205)])
trials = [
    holder,
    trial('B', '试验B', [read(20, hold=10)]),                  # 无期限，窗口关闭仍排队 -> closed
    trial('C', '试验C', [read(30, hold=10, latest=200)]),      # 期限=horizon，等不到槽 -> expired
    trial('D', '试验D', [read(40, hold=10, latest=199)]),      # 期限严格早于时限 -> expired
]
res = run(nodes, trials, w)
check('holder 在时限内启动，占用可越出窗口', ledger_of(res, 'H')[0]['start_ms'] == 5
      and ledger_of(res, 'H')[0]['end_ms'] == 210)
check('B 窗口关闭仍排队 -> closed', ledger_of(res, 'B')[0]['status'] == 'closed', str(ledger_of(res, 'B')[0]))
check('D 期限 199 先过期 -> expired', ledger_of(res, 'D')[0]['status'] == 'expired')
check('C 期限=horizon 仍等不到容量 -> expired',
      ledger_of(res, 'C')[0]['status'] == 'expired', str(ledger_of(res, 'C')[0]))
check('无任何启动时刻越过 horizon', all(e['start_ms'] <= 200 for e in res['read_ledger']
                                          if e['status'] == 'scheduled'))
# 闭区间：holder 恰在 horizon 时刻释放，同刻等待者仍可启动（释放事件先于关闭事件）
w2 = window(horizon=110)
res2 = run(single, [trial('H', '占座', [read(5, hold=105)]),
                    trial('B', '试验B', [read(20, hold=5)])], w2)
check('B 在 110=horizon 时刻仍可启动（闭区间）',
      ledger_of(res2, 'B')[0]['status'] == 'scheduled' and ledger_of(res2, 'B')[0]['start_ms'] == 110,
      str(ledger_of(res2, 'B')[0]))
assert_capacity_invariant(res, nodes)

print('\nT9 每节点容量独立 / 主库回退容量')
nodes = base_nodes(p_cap=2, r1_cap=1, r2_cap=1)
# R1、R2 各1槽，主库2槽。前4个读：R1、R2 各1，主库回退2；第5个读超出全部容量 -> 排队
trials = [trial('A', '试验A', [read(t, hold=100) for t in (10, 12, 14, 16)])]
res = run(nodes, trials, window())
le = ledger_of(res, 'A')
check('4 个读全部调度（2副本+2主库），无排队', all(e['status'] == 'scheduled' for e in le), str(le))
check('主库承接 2 个回退读', len(entries_at_node(res, 'P')) == 2)
trials2 = [trial('A', '试验A', [read(t, hold=100) for t in (10, 12, 14, 16, 18)])]
res2 = run(nodes, trials2, window())
le2 = ledger_of(res2, 'A')
check('第 5 个读等待，最早释放点(110)后启动', le2[4]['status'] == 'scheduled' and le2[4]['start_ms'] >= 110, str(le2[4]))
# 单节点打满不影响另一节点：R1 长占，新读仍可走空闲 R2
nodes3 = base_nodes(p_cap=10, r1_cap=1, r2_cap=5)
res3 = run(nodes3, [trial('A', '试验A', [read(5, hold=400), read(10, hold=10)])], window())
check('R1 打满后新读改走空闲 R2，互不影响',
      ledger_of(res3, 'A')[1]['node'] == 'R2' and ledger_of(res3, 'A')[1]['start_ms'] == 10)
assert_capacity_invariant(res, nodes)
assert_capacity_invariant(res2, nodes)
assert_capacity_invariant(res3, nodes3)

print('\nT10 等待期间主库故障与恢复')
# R1、R2 均被 holder 长占；P 在 15 先下线，B 读20 到达时只能排队，80 恢复后回退主库
nodes = base_nodes(p_cap=10, r1_cap=1, r2_cap=1)
holder = trial('H', '占座试验', [read(5, hold=400), read(6, hold=400)])
trials = [
    holder,
    trial('B', '试验B', [{'at_ms': 15, 'kind': 'node_down', 'node': 'P'},
                         read(20, hold=30),
                         {'at_ms': 80, 'kind': 'node_up', 'node': 'P'}]),
]
res = run(nodes, trials, window())
lb = ledger_of(res, 'B')
check('主库故障期间 B 持续排队，80 恢复后走主库', lb[0]['node'] == 'P' and lb[0]['start_ms'] == 80
      and lb[0]['wait_ms'] == 60, str(lb[0]))
assert_capacity_invariant(res, nodes)

print('\nT11 结构校验 / 会话隔离 / 确定性')
bad_cases = [
    ('节点容量非正', [{**n, 'read_capacity': 0} if n['name'] == 'R1' else n for n in base_nodes()],
     [trial('A', 'A', [read(0)])], window()),
    ('期限早于到达', base_nodes(), [trial('A', 'A', [read(10, latest=5)])], window()),
    ('事件越出窗口', base_nodes(), [trial('A', 'A', [read(6000)])], window(horizon=5000)),
    ('取消晚于时限', base_nodes(), [trial('A', 'A', [read(0)], cancel=6000)], window(horizon=5000)),
    ('期限晚于时限', base_nodes(), [trial('A', 'A', [read(0, latest=6000)])], window(horizon=5000)),
    ('非读事件带占用', base_nodes(), [trial('A', 'A', [{'at_ms': 0, 'kind': 'write', 'hold_ms': 5}])], window()),
    ('空窗口', base_nodes(), [], window()),
]
for label, ns, ts, w in bad_cases:
    try:
        validate_input(ns, ts, w)
        check('拒绝非法输入：' + label, False)
    except ValueError:
        check('拒绝非法输入：' + label, True)
# 会话间写后读隔离：A 写后不影响 B 的 required；同试验同会话必须读到足够位点
nodes = base_nodes(r1_cap=10)
trials = [
    trial('A', '试验A', [write(0, 's1'), read(10, 's1', hold=10)], sticky=0),
    trial('B', '试验B', [read(10, 's2', hold=10)], sticky=0),
]
res = run(nodes, trials, window())
check('A 写后读位点需求=101', ledger_of(res, 'A')[0]['required_position'] == 101)
check('B 独立会话无写入，位点需求=0（会话隔离）', ledger_of(res, 'B')[0]['required_position'] == 0)
check('全部调度读安全', res['summary']['all_scheduled_reads_safe'] is True)
check('checksum 存在且为64位', len(res['checksum']) == 64)
# 每个读都有完整去向台账
check('台账覆盖全部读请求', len(res['read_ledger']) == 2
      and all(set(('seq', 'trial_id', 'status', 'node', 'start_ms', 'end_ms', 'wait_ms',
                   'hold_ms', 'latest_start_ms', 'reason')) <= set(e) for e in res['read_ledger']))

print('\n================ 结果 ================')
print('通过 %d 项，失败 %d 项' % (len(PASSED), len(FAILED)))
if FAILED:
    sys.exit(1)
print('全部场景验证通过。')
