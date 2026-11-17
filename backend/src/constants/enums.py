ROLES = ('admin', 'operator', 'viewer')
RUN_STATES = ('queued', 'running', 'completed', 'failed', 'cancelled')
WINDOW_STATES = ('open', 'running', 'completed', 'failed')
# 窗口内单个读请求的去向状态：排队等待 / 已调度占用 / 过期拒绝 / 窗口关闭拒绝 / 取消撤回
READ_STATES = ('waiting', 'scheduled', 'expired', 'closed', 'withdrawn')
