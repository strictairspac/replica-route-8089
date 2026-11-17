"""共享演练窗口引擎：多试验共用同一条时间轴与节点读容量的确定性离散事件重放。

设计口径（与单试验 routing.simulate 的写后读约束保持一致，新增容量/排队/期限/取消语义）：
1. 节点可设 read_capacity（并发读上限）；读请求被路由到节点即占用一个槽位，占用 hold_ms 后释放。
2. 读请求可自带 hold_ms 与 latest_start_ms（时间轴上的绝对最迟开始时刻）；缺省 hold 用窗口默认值。
3. 容量不足时请求进入共享 FIFO 等待队列，不抢占其他试验已占的剩余容量；可回退主库时按序选路。
4. 等待期间发生的 replicate / node_down / node_up 在重新调度时即时影响选路与容量。
5. latest_start_ms 边界：启动时刻 at <= latest_start_ms 仍可启动，严格大于才判过期拒绝（闭区间）。
6. 取消试验：尚未开始的排队读立即撤回且不占容量；已开始的占用保留到自然结束；已完成决策不改写。
7. 全部输入确定则输出确定（含 checksum），重试同一入参不会重复占用容量。
"""
import copy
import hashlib
import heapq
import itertools
import json

RANK_ARRIVE = 0    # 时序事件到达
RANK_RELEASE = 1   # 读占用到期释放
RANK_CANCEL = 2    # 试验取消点
RANK_DRAIN = 3     # 重新调度等待队列
RANK_EXPIRE = 4    # 最迟开始期限到期
RANK_HORIZON = 5   # 窗口时限到达，关闭仍在排队的读

TOPO_KINDS = ('replicate', 'node_down', 'node_up')


def validate_input(nodes, trials, window):
    """结构性校验，供发起试验前预检与引擎运行时复用。"""
    if not 1 <= len(nodes) <= 12:
        raise ValueError('需要1–12个节点')
    if len({x['name'] for x in nodes}) != len(nodes):
        raise ValueError('节点名必须唯一')
    primaries = [x for x in nodes if x['role'] == 'primary']
    if len(primaries) != 1:
        raise ValueError('必须且只能配置一个主节点')
    lineage = primaries[0]['lineage']
    if any(n['role'] == 'replica' and n['lineage'] == lineage
           and n['position'] > primaries[0]['position'] for n in nodes):
        raise ValueError('同日志谱系副本位点不得高于主库初始位点')
    if any(int(n.get('read_capacity', 1)) < 1 for n in nodes):
        raise ValueError('节点读容量必须为正整数')
    if int(window['default_hold_ms']) < 0 or int(window['horizon_ms']) < 0:
        raise ValueError('默认占用时长与窗口时限不得为负')
    if not trials:
        raise ValueError('窗口内至少需要一个试验')
    seen = set()
    for trial in trials:
        if not 1 <= len(trial['events']) <= 100:
            raise ValueError('每个试验需要1–100个时序事件')
        last_time = -1
        if trial.get('cancel_at_ms') is not None:
            if trial['cancel_at_ms'] > window['horizon_ms']:
                raise ValueError('取消时刻不得晚于窗口时限')
        for ev in trial['events']:
            if ev['at_ms'] > window['horizon_ms']:
                raise ValueError('事件时刻不得晚于窗口时限')
            at = ev['at_ms']
            if at < last_time:
                raise ValueError('事件必须按时间非递减排列')
            last_time = at
            if ev['kind'] == 'read':
                if ev.get('hold_ms') is not None and ev['hold_ms'] < 0:
                    raise ValueError('读占用时长不得为负')
                if ev.get('latest_start_ms') is not None:
                    if ev['latest_start_ms'] < at:
                        raise ValueError('最迟开始时间不得早于请求到达时间')
                    if ev['latest_start_ms'] > window['horizon_ms']:
                        raise ValueError('最迟开始时间不得晚于窗口时限')
            elif ev.get('hold_ms') is not None or ev.get('latest_start_ms') is not None:
                raise ValueError('占用时长与最迟开始时间仅允许读请求设置')
        if trial.get('cancel_at_ms') is not None and trial['cancel_at_ms'] < 0:
            raise ValueError('取消时刻不得为负')
        key = trial.get('id', trial['name'])
        if key in seen:
            raise ValueError('窗口内试验标识必须唯一')
        seen.add(key)


def _replica_checks(node_map, lineage, required, max_delay):
    return [{'name': n['name'], 'healthy': n['status'] == 'healthy',
             'same_lineage': n['lineage'] == lineage,
             'position_sufficient': n['position'] >= required,
             'delay_within_limit': n['delay_ms'] <= max_delay}
            for n in node_map.values() if n['role'] == 'replica']


def run(nodes, trials, window):
    """执行共享窗口重放，返回窗口级结果字典。nodes/trials/window 均为普通字典。"""
    validate_input(nodes, trials, window)
    default_hold = int(window['default_hold_ms'])
    horizon = int(window['horizon_ms'])
    work = copy.deepcopy(nodes)
    node_map = {x['name']: x for x in work}
    primary = next(x for x in work if x['role'] == 'primary')
    lineage = primary['lineage']
    primary_name = primary['name']

    sessions = {}       # (trial_id, session) -> 会话写后读状态（试验间隔离）
    active = []         # 已开始的读占用
    pending = []        # 等待中的读条目，即共享 FIFO 队列
    ledger = []         # 每个读请求一条去向台账（与 pending/entry 同源对象）
    trace = []
    heap = []
    tie = itertools.count()  # 同刻同类事件的稳定次序（入堆顺序 = 试验/事件输入顺序）
    drain_scheduled = False
    peak_queue = 0
    peak_in_flight = 0

    def state_of(trial_id, session):
        return sessions.get((trial_id, session),
                            {'required': 0, 'lineage': lineage, 'last_write_ms': None})

    def schedule_drain(when):
        nonlocal drain_scheduled
        if not drain_scheduled:
            drain_scheduled = True
            heapq.heappush(heap, (when, RANK_DRAIN, next(tie), 'drain', None))

    def in_flight(node_name, now):
        return sum(1 for o in active if o['node'] == node_name and o['end_ms'] > now)

    def free_slot(node_name, now):
        return in_flight(node_name, now) < int(node_map[node_name].get('read_capacity', 1))

    def pick_node(entry, now):
        """写后读策略 + 共享容量选路；选不出返回 None，请求继续排队。"""
        st = state_of(entry['trial_id'], entry['session'])
        required = st['required']
        max_delay = entry['trial']['max_delay_ms']
        sticky = st['last_write_ms'] is not None and now - st['last_write_ms'] < entry['trial']['sticky_ms']
        if sticky:
            if (primary['status'] == 'healthy' and primary['lineage'] == st['lineage']
                    and primary['position'] >= required and free_slot(primary_name, now)):
                return primary, '写后读粘滞：保持主库（共享窗口尚有读容量）'
            return None
        eligible = [n for n in node_map.values()
                    if n['role'] == 'replica' and n['status'] == 'healthy'
                    and n['lineage'] == st['lineage'] and n['position'] >= required
                    and n['delay_ms'] <= max_delay]
        eligible.sort(key=lambda n: (n['delay_ms'], -n['position'], n['name']))
        for n in eligible:
            if free_slot(n['name'], now):
                return n, '共享窗口容量可用：副本位点满足会话且延迟达标'
        if (primary['status'] == 'healthy' and primary['lineage'] == st['lineage']
                and primary['position'] >= required and free_slot(primary_name, now)):
            return primary, '无空闲达标副本，回退主库'
        return None

    def do_drain(now):
        nonlocal peak_queue, peak_in_flight
        remaining = []
        for entry in pending:
            if entry['status'] != 'waiting':
                continue
            picked = pick_node(entry, now)
            if not picked:
                remaining.append(entry)
                continue
            node, reason = picked
            entry.update(status='scheduled', node=node['name'], start_ms=now,
                         end_ms=now + entry['hold_ms'], wait_ms=now - entry['at_ms'],
                         observed_position=node['position'],
                         safe=node['position'] >= entry['required_position']
                         and node['lineage'] == entry['lineage'], reason=reason)
            active.append({'end_ms': entry['end_ms'], 'node': node['name'],
                           'trial_id': entry['trial_id'], 'seq': entry['seq']})
            heapq.heappush(heap, (entry['end_ms'], RANK_RELEASE, next(tie), 'release', entry['seq']))
            row = entry['trace_row']
            row.update(node=node['name'], decision='routed', reason=reason,
                       observed_position=node['position'], safe=entry['safe'],
                       start_ms=now, end_ms=entry['end_ms'], wait_ms=entry['wait_ms'],
                       replica_checks=_replica_checks(node_map, entry['lineage'],
                                                      entry['required_position'],
                                                      entry['trial']['max_delay_ms']))
        pending[:] = remaining
        peak_queue = max(peak_queue, len(pending))
        peak_in_flight = max(peak_in_flight, sum(1 for o in active if o['end_ms'] > now))

    # 所有试验的事件与取消点进入同一个堆 —— 窗口内共用一条时间轴
    heapq.heappush(heap, (int(horizon), RANK_HORIZON, next(tie), 'horizon', None))
    for trial in trials:
        trial_id = trial.get('id', trial['name'])
        if trial.get('cancel_at_ms') is not None:
            heapq.heappush(heap, (int(trial['cancel_at_ms']), RANK_CANCEL, next(tie), 'cancel', trial_id))
        for index, ev in enumerate(trial['events']):
            heapq.heappush(heap, (int(ev['at_ms']), RANK_ARRIVE, next(tie), 'arrive',
                                  {'trial': trial, 'trial_id': trial_id, 'index': index, 'event': ev}))

    canceled = set()
    seq_cursor = 0
    while heap:
        now, rank, _tie, kind, payload = heapq.heappop(heap)
        if kind == 'release':
            active[:] = [o for o in active if not (o['seq'] == payload and o['end_ms'] <= now)]
            schedule_drain(now)
        elif kind == 'cancel':
            canceled.add(payload)
            for entry in pending:
                if entry['status'] == 'waiting' and entry['trial_id'] == payload:
                    entry.update(status='withdrawn',
                                 reason='试验取消：读取尚未开始，撤回排队且不占用容量')
                    entry['trace_row'].update(decision='withdrawn', reason=entry['reason'])
            pending[:] = [e for e in pending if e['status'] == 'waiting']
            trace.append({'at_ms': now, 'kind': 'trial_cancel', 'session': '-', 'node': None,
                          'decision': 'applied', 'trial_id': payload, 'trial_name': payload,
                          'required_position': None, 'observed_position': None, 'safe': True,
                          'reason': '试验取消点到达：排队读撤回，已开始占用保留至结束'})
            schedule_drain(now)
        elif kind == 'drain':
            drain_scheduled = False
            do_drain(now)
        elif kind == 'expire':
            entry = payload
            if entry['status'] == 'waiting':
                entry.update(status='expired',
                             reason='超过最迟开始时间 %sms 仍未获得容量，过期拒绝' % entry['latest_start_ms'])
                entry['trace_row'].update(decision='rejected', reason=entry['reason'],
                                          replica_checks=_replica_checks(
                                              node_map, entry['lineage'], entry['required_position'],
                                              entry['trial']['max_delay_ms']))
                pending[:] = [e for e in pending if e['status'] == 'waiting']
                schedule_drain(now)
        elif kind == 'horizon':
            # 窗口时限到达：仍在排队的读全部关闭，之后即使有释放也不得再启动
            for entry in pending:
                if entry['status'] == 'waiting':
                    reason = '窗口关闭（%sms）时仍在排队，拒绝读取' % horizon
                    entry.update(status='closed', reason=reason)
                    entry['trace_row'].update(decision='rejected', reason=reason,
                                              replica_checks=_replica_checks(
                                                  node_map, entry['lineage'], entry['required_position'],
                                                  entry['trial']['max_delay_ms']))
            pending[:] = [e for e in pending if e['status'] == 'waiting']
        elif kind == 'arrive':
            trial, trial_id, ev = payload['trial'], payload['trial_id'], payload['event']
            if trial_id in canceled:
                continue  # 取消后的事件全部跳过，已完成决策不改写
            at, ev_kind, session = ev['at_ms'], ev['kind'], ev.get('session', 'default')
            row = {'index': len(trace) + 1, 'at_ms': at, 'kind': ev_kind, 'session': session,
                   'node': None, 'decision': 'applied', 'reason': '', 'trial_id': trial_id,
                   'trial_name': trial.get('name', str(trial_id)),
                   'required_position': state_of(trial_id, session)['required'],
                   'observed_position': None, 'safe': True}
            if ev_kind in TOPO_KINDS:
                target = node_map.get(ev.get('node'))
                if not target:
                    raise ValueError('事件引用未知节点：' + str(ev.get('node')))
                row['node'] = target['name']
                if ev_kind == 'replicate':
                    if target['role'] != 'replica':
                        raise ValueError('replicate仅允许副本节点')
                    if target['status'] != 'healthy':
                        raise ValueError('停机副本不能应用日志，请先node_up')
                    if target['lineage'] != lineage:
                        raise ValueError('日志谱系不匹配，不得复制当前主库日志')
                    pos, delay = ev.get('position'), ev.get('delay_ms')
                    if pos is None or delay is None or delay < 0 \
                            or not target['position'] <= pos <= primary['position']:
                        raise ValueError('复制位点必须单调且不超过已提交主库位点')
                    target['position'], target['delay_ms'] = pos, delay
                    row['observed_position'] = pos
                    row['reason'] = '副本应用已提交日志（共享时间轴，影响所有等待中的选路）'
                else:
                    target['status'] = 'down' if ev_kind == 'node_down' else 'healthy'
                    row['reason'] = '节点下线，等待中的读将重新选路' if ev_kind == 'node_down' \
                        else '节点恢复，等待中的读可重新选路'
                trace.append(row)
                schedule_drain(at)
            elif ev_kind == 'write':
                if primary['status'] != 'healthy':
                    row.update(decision='rejected', reason='主库不可用，拒绝写入')
                else:
                    primary['position'] += 1
                    sessions[(trial_id, session)] = {'required': primary['position'],
                                                     'lineage': lineage, 'last_write_ms': at}
                    row.update(node=primary_name, decision='routed', reason='写入主库并记录会话位点',
                               required_position=primary['position'], observed_position=primary['position'])
                trace.append(row)
            elif ev_kind == 'read':
                st = state_of(trial_id, session)
                hold = int(ev['hold_ms']) if ev.get('hold_ms') is not None else default_hold
                deadline = ev.get('latest_start_ms')
                seq_cursor += 1
                row.update(decision='queued', hold_ms=hold, latest_start_ms=deadline, wait_ms=None,
                           reason='读请求进入共享窗口等待队列，等待容量或达标节点',
                           replica_checks=_replica_checks(node_map, st['lineage'],
                                                          st['required'], trial['max_delay_ms']))
                trace.append(row)
                entry = {'seq': seq_cursor, 'trial_id': trial_id,
                         'trial_name': trial.get('name', str(trial_id)), 'trial': trial,
                         'session': session, 'at_ms': at, 'hold_ms': hold,
                         'latest_start_ms': deadline, 'required_position': st['required'],
                         'lineage': st['lineage'], 'status': 'waiting', 'node': None,
                         'start_ms': None, 'end_ms': None, 'wait_ms': None,
                         'observed_position': None, 'safe': False, 'reason': row['reason'],
                         'trace_row': row}
                pending.append(entry)
                ledger.append(entry)
                if deadline is not None:
                    heapq.heappush(heap, (int(deadline), RANK_EXPIRE, next(tie), 'expire', entry))
                schedule_drain(at)
            else:
                raise ValueError('未知事件类型')

    # 窗口时限到达仍在排队的读：关闭拒绝，不允许越过 horizon
    for entry in pending:
        if entry['status'] == 'waiting':
            reason = '窗口关闭（%sms）时仍在排队，拒绝读取' % horizon
            entry.update(status='closed', reason=reason)
            entry['trace_row'].update(decision='rejected', reason=reason,
                                      replica_checks=_replica_checks(
                                          node_map, entry['lineage'], entry['required_position'],
                                          entry['trial']['max_delay_ms']))

    reads = [r for r in trace if r['kind'] == 'read']
    scheduled_rows = [r for r in reads if r['decision'] == 'routed']
    rejected_rows = [r for r in reads if r['decision'] == 'rejected']
    withdrawn_rows = [r for r in reads if r['decision'] == 'withdrawn']
    per_trial = []
    for trial in trials:
        tid = trial.get('id', trial['name'])
        t_trace = [r for r in trace if r.get('trial_id') == tid]
        t_ledger = [dict((k, v) for k, v in e.items() if k != 'trace_row' and k != 'trial')
                    for e in ledger if e['trial_id'] == tid]
        t_sched = [r for r in t_ledger if r['status'] == 'scheduled']
        per_trial.append({'trial_id': tid, 'name': trial.get('name', str(tid)),
                          'status': 'cancelled' if tid in canceled else 'completed',
                          'cancel_at_ms': trial.get('cancel_at_ms'),
                          'summary': {'requests': len([r for r in t_trace if r['kind'] in ('read', 'write')]),
                                      'reads': len(t_ledger), 'scheduled': len(t_sched),
                                      'expired': sum(r['status'] == 'expired' for r in t_ledger),
                                      'withdrawn': sum(r['status'] == 'withdrawn' for r in t_ledger),
                                      'closed': sum(r['status'] == 'closed' for r in t_ledger),
                                      'replica_reads': sum(r['node'] != primary_name for r in t_sched),
                                      'primary_reads': sum(r['node'] == primary_name for r in t_sched),
                                      'max_wait_ms': max((r['wait_ms'] or 0 for r in t_ledger), default=0),
                                      'all_scheduled_safe': all(r['safe'] for r in t_sched)},
                          'read_ledger': t_ledger, 'trace': t_trace})
    result = {'mode': '共享演练窗口 / 多试验共用时间轴与节点读容量 / 非真实复制集群',
              'lineage': lineage,
              'window': {'id': window.get('id'), 'name': window.get('name'),
                         'default_hold_ms': default_hold, 'horizon_ms': horizon},
              'trace': trace,
              'read_ledger': [dict((k, v) for k, v in e.items() if k not in ('trace_row', 'trial'))
                              for e in ledger],
              'sessions': {'%s|%s' % (k[0], k[1]): v for k, v in sessions.items()},
              'final_nodes': work, 'trial_results': per_trial,
              'summary': {'trials': len(trials), 'cancelled_trials': len(canceled),
                          'reads': len(reads), 'scheduled': len(scheduled_rows),
                          'rejected': len(rejected_rows), 'withdrawn': len(withdrawn_rows),
                          'expired': sum(e['status'] == 'expired' for e in ledger),
                          'closed_unrouted': sum(e['status'] == 'closed' for e in ledger),
                          'replica_reads': sum(r['decision'] == 'routed' and r['node'] != primary_name
                                               for r in reads),
                          'primary_reads': sum(r['decision'] == 'routed' and r['node'] == primary_name
                                               for r in reads),
                          'peak_queue_depth': peak_queue, 'peak_in_flight': peak_in_flight,
                          'all_scheduled_reads_safe': all(r['safe'] for r in scheduled_rows)},
              'policy': {'capacity_model': '每节点 read_capacity 个并发读槽，读启动即占用，hold_ms 后释放',
                         'queue_model': '跨试验共享 FIFO 等待队列，不抢占其他试验已占容量',
                         'deadline_boundary': '启动时刻 <= latest_start_ms 可启动，严格大于才过期拒绝',
                         'hold_source': '读事件 hold_ms 优先，缺省取窗口 default_hold_ms',
                         'cancel_rule': '排队读立即撤回不占容量；已开始占用保留至 end_ms；已完成决策不改写',
                         'sticky_boundary': 'now - last_write_ms < sticky_ms',
                         'replica_tie_break': 'delay升序 / position降序 / name字典序',
                         'event_tie_break': '同刻事件按 到达<释放<取消<调度<期限 次序，到达再按试验/序号'}}
    result['checksum'] = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result
