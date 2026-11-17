// 前端渲染冒烟测试：最小 localStorage 桩 + 模拟数据，验证窗口页面无运行时错误且包含关键信息。
// 运行：node --check 已过；本脚本 node tests/render_smoke.mjs
globalThis.localStorage={
  _d:{'replicaroute-user':JSON.stringify({email:'operator@qiyuan.test',role:'operator'})},
  getItem(k){return this._d[k]??null;},setItem(k,v){this._d[k]=v;},removeItem(k){delete this._d[k];}
};
const {windowsPage,windowDetailPage}=await import('../src/pages/windows.js');

const nodes=[
  {id:1,name:'P',role:'primary',status:'healthy',lineage:'L1',position:100,delay_ms:0,read_capacity:1,note:''},
  {id:2,name:'R1',role:'replica',status:'healthy',lineage:'L1',position:100,delay_ms:20,read_capacity:1,note:''},
];
const sets=[{id:1,code:'RS-X',title:'争抢集',events:[]}];
const windows=[
  {id:1,name:'开放窗口',status:'open',default_hold_ms:100,horizon_ms:5000,created_at:new Date().toISOString(),
   config_snapshot:{trials:[]},result:null,trials:[]},
  {id:2,name:'完成窗口',status:'completed',default_hold_ms:100,horizon_ms:5000,created_at:new Date().toISOString(),
   result:{checksum:'abcdef0123456789',summary:{trials:2,scheduled:3,expired:1,withdrawn:1,closed_unrouted:0,
     reads:5,cancelled_trials:1,replica_reads:1,primary_reads:2,peak_queue_depth:2,peak_in_flight:1,
     all_scheduled_reads_safe:true},window:{default_hold_ms:100,horizon_ms:5000},lineage:'L1',
     final_nodes:nodes,
     read_ledger:[{seq:1,trial_id:1,trial_name:'试验A',at_ms:10,latest_start_ms:null,hold_ms:100,
       status:'scheduled',node:'P',start_ms:10,end_ms:110,wait_ms:0,reason:'回退主库'},
       {seq:2,trial_id:2,trial_name:'试验B',at_ms:15,latest_start_ms:null,hold_ms:80,
       status:'withdrawn',node:null,start_ms:null,end_ms:null,wait_ms:null,reason:'取消撤回'}],
     trace:[{at_ms:10,kind:'read',trial_name:'试验A',session:'s',decision:'routed',node:'P',reason:'回退主库'},
            {at_ms:50,kind:'trial_cancel',trial_name:'试验B',session:'-',decision:'applied',node:null,reason:'取消点'}],
     trial_results:[{trial_id:1,name:'试验A',status:'completed',cancel_at_ms:null,
       summary:{scheduled:1,expired:0,withdrawn:0,closed:0,replica_reads:0,primary_reads:1,max_wait_ms:0,all_scheduled_safe:true}}],
     policy:{capacity_model:'容量',queue_model:'队列',deadline_boundary:'期限',cancel_rule:'取消'},
     mode:'共享演练窗口'}},
];

let pass=0,fail=0;
const ok=(name,cond)=>{cond?pass++:(fail++,console.log('  ✗',name));console.log('  '+(cond?'✓':'✗'),name);};

const listHtml=windowsPage(windows,sets,nodes);
ok('列表页含窗口配置表单',listHtml.includes('id="window-form"'));
ok('列表页含读容量提示',listHtml.includes('读容量'));
ok('列表页含共享调度口径',listHtml.includes('FIFO')&&listHtml.includes('抢占'));
ok('列表页含已关闭窗口区',listHtml.includes('已关闭窗口'));
ok('列表页转义节点名',listHtml.includes('P')&&listHtml.includes('R1'));

const openHtml=windowDetailPage(windows[0],sets);
ok('开放窗口含发起试验表单',openHtml.includes('id="trial-window-form"'));
ok('开放窗口含执行按钮',openHtml.includes('id="execute-window"'));
ok('开放窗口含取消点输入',openHtml.includes('cancel_at_ms'));

const doneHtml=windowDetailPage(windows[1],sets);
ok('完成窗口含去向台账',doneHtml.includes('等待读取去向台账'));
ok('完成窗口含共享时间轴',doneHtml.includes('共享时间轴事件'));
ok('完成台账渲染 scheduled 徽章',doneHtml.includes('read-scheduled'));
ok('完成台账渲染 withdrawn 徽章',doneHtml.includes('read-withdrawn'));
ok('完成窗口含 checksum',doneHtml.includes('abcdef0123456789'));
ok('完成窗口含导出按钮',doneHtml.includes('id="window-export"'));
ok('完成窗口不含发起表单',!doneHtml.includes('id="trial-window-form"'));

// viewer 角色：窗口创建/执行按钮应禁用
globalThis.localStorage._d['replicaroute-user']=JSON.stringify({email:'v@t',role:'viewer'});
const viewerHtml=windowsPage(windows,sets,nodes);
const createBtn=viewerHtml.match(/<button[^>]*>创建窗口 \+<\/button>/)?.[0]||'';
ok('viewer 创建窗口按钮禁用',createBtn.includes('disabled'));
const viewerDetail=windowDetailPage(windows[0],sets);
ok('viewer 详情页执行按钮禁用或不渲染',!viewerDetail.includes('id="execute-window"')||
  viewerDetail.match(/<button[^>]*id="execute-window"[^>]*>/)?.[0]?.includes('disabled'));

console.log('\n前端渲染冒烟：通过 %d，失败 %d',pass,fail);
process.exit(fail?1:0);
