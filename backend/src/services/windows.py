"""共享演练窗口服务：窗口配置、窗口内试验发起/取消、确定性重放与证据落库。"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from src.models.entities import Audit, Node, RequestSet, Trial, Window
from src.utils.serialization import record
from src.services.window_engine import run as engine_run, validate_input
from .common import save


def _now():
    return datetime.now(timezone.utc)


def _conflict(db, status, message):
    """显式拒绝前回滚当前事务，避免把调用方会话留在失败态。"""
    db.rollback()
    raise HTTPException(status, message)


def listing(db):
    return [record(x) for x in db.scalars(select(Window).order_by(Window.id.desc()))]


def detail(db, ident):
    obj = db.get(Window, ident)
    if not obj:
        raise HTTPException(404, '演练窗口不存在')
    data = record(obj)
    data['trials'] = [record(x) for x in db.scalars(
        select(Trial).where(Trial.window_id == ident).order_by(Trial.id))]
    return data


def create(db, data, user):
    obj = Window(name=data.name, status='open',
                 default_hold_ms=data.default_hold_ms, horizon_ms=data.horizon_ms,
                 config_snapshot={'nodes': [], 'trials': []})
    return save(db, obj, user, '创建共享演练窗口 ' + data.name)


def _open_window(db, ident):
    obj = db.get(Window, ident)
    if not obj:
        _conflict(db, 404, '演练窗口不存在')
    if obj.status != 'open':
        _conflict(db, 409, '窗口已%s，不能再发起或取消试验' % obj.status)
    return obj


def add_trial(db, ident, data, user):
    window = _open_window(db, ident)
    req = db.get(RequestSet, data.request_set_id)
    if not req:
        _conflict(db, 404, '请求集不存在')
    if data.cancel_at_ms is not None and data.cancel_at_ms > window.horizon_ms:
        _conflict(db, 422, '取消时刻不得晚于窗口时限')
    if any(ev['at_ms'] > window.horizon_ms for ev in req.events):
        _conflict(db, 422, '请求集存在晚于窗口时限的事件，请更换请求集或延长窗口')
    nodes = [record(x) for x in db.scalars(select(Node).order_by(Node.id))]
    snapshot = {'nodes': nodes, 'request_set': record(req)}
    obj = Trial(name=data.name, request_set_id=data.request_set_id, window_id=window.id,
                max_delay_ms=data.max_delay_ms, sticky_ms=data.sticky_ms,
                cancel_at_ms=data.cancel_at_ms, status='queued',
                input_snapshot=snapshot)
    return save(db, obj, user, '窗口%s发起试验 %s' % (window.id, data.name))


def cancel_trial(db, window_id, trial_id, data, user):
    window = _open_window(db, window_id)
    obj = db.get(Trial, trial_id)
    if not obj or obj.window_id != window.id:
        _conflict(db, 404, '窗口内试验不存在')
    if obj.status != 'queued':
        _conflict(db, 409, '试验已%s，取消只接受排队中的试验' % obj.status)
    if data.cancel_at_ms > window.horizon_ms:
        _conflict(db, 422, '取消时刻不得晚于窗口时限')
    obj.cancel_at_ms = data.cancel_at_ms
    obj.status = 'cancelled'
    return save(db, obj, user, '窗口%s取消试验 %s @%sms' % (window.id, obj.name, data.cancel_at_ms))


def _build_engine_input(db, window):
    rows = db.scalars(select(Trial).where(Trial.window_id == window.id).order_by(Trial.id)).all()
    if not rows:
        raise ValueError('窗口内还没有试验，先发起至少一次试验')
    node_rows = db.scalars(select(Node).order_by(Node.id)).all()
    nodes = [record(x) for x in node_rows]
    trials = []
    for t in rows:
        item = {'id': t.id, 'name': t.name,
                'max_delay_ms': t.max_delay_ms, 'sticky_ms': t.sticky_ms,
                'events': t.input_snapshot['request_set']['events']}
        # 发起时自带的时间轴取消点，与显式取消接口写入的取消点都要传给引擎
        if t.cancel_at_ms is not None:
            item['cancel_at_ms'] = t.cancel_at_ms
        trials.append(item)
    window_cfg = {'id': window.id, 'name': window.name,
                  'default_hold_ms': window.default_hold_ms, 'horizon_ms': window.horizon_ms}
    return nodes, trials, window_cfg, rows


def execute(db, ident, user):
    window = _open_window(db, ident)
    try:
        nodes, trials, window_cfg, rows = _build_engine_input(db, window)
        validate_input(nodes, trials, window_cfg)
        result = engine_run(nodes, trials, window_cfg)
    except HTTPException:
        raise
    except ValueError as e:
        db.rollback()
        window = db.get(Window, ident)
        window.status = 'failed'
        window.error = str(e)
        window.closed_at = _now()
        db.commit()
        raise HTTPException(422, str(e))
    window.status = 'completed'
    window.closed_at = _now()
    window.result = result
    window.config_snapshot = {'nodes': nodes, 'trials': trials}
    per_trial = {t['trial_id']: t for t in result['trial_results']}
    for t in rows:
        piece = per_trial.get(t.id)
        t.result = piece
        # 经取消接口撤回的试验保持 cancelled；其余随窗口完成（时间轴取消点只影响其排队读）
        t.status = 'cancelled' if t.status == 'cancelled' else 'completed'
    db.add(Audit(actor=user.email,
                 action='执行共享窗口 %s（%d 个试验）' % (window.name, len(rows))))
    db.commit()
    return detail(db, ident)
