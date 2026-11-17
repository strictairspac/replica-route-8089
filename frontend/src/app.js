import {api,download,downloadWindow} from './api/client.js';
import {nodesStore} from './stores/nodes.js';
import {request_setsStore} from './stores/request_sets.js';
import {trialsStore} from './stores/trials.js';
import {windowsStore} from './stores/windows.js';
import {windowsApi} from './api/windows.js';
import {session} from './hooks/session.js';
import {feedback} from './hooks/feedback.js';
import {formValues} from './utils/forms.js';
import {esc,date} from './utils/html.js';
import {frame} from './components/common/frame.js';
import {table} from './components/common/table.js';
import {loginPage} from './pages/login.js';
import {nodesPage} from './pages/nodes.js';
import {requestSetsPage} from './pages/request_sets.js';
import {trialsPage} from './pages/trials.js';
import {evidencePage} from './pages/evidence.js';
import {windowsPage,windowDetailPage} from './pages/windows.js';
import {titles} from './router/routes.js';

let timer=null;const app=document.querySelector('#app');
function bindForm(id,path,transform,after){document.querySelector(id)?.addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const data=transform(formValues(e.target));const ident=data.id;delete data.id;const result=await api(path+(ident?'/'+ident:''),ident?'PUT':'POST',data);await after(result);feedback('保存成功，记录已更新。')}catch(err){feedback(err.message.includes('JSON')?'事件JSON格式错误，请检查引号与逗号':err.message,true)}finally{b.disabled=false}})}
async function render(){
  clearTimeout(timer);
  if(!session.token){app.innerHTML=loginPage();bindForm('#login','/auth/login',x=>x,async x=>{session.save(x);location.hash='nodes';await render()});return}
  try{
    await api('/auth/me');
    await Promise.all([nodesStore.refresh(),request_setsStore.refresh(),trialsStore.refresh(),windowsStore.refresh()]);
    const [page='nodes',id]=location.hash.slice(1).split('/');
    const nodes=nodesStore.items,sets=request_setsStore.items,trials=trialsStore.items,windows=windowsStore.items;
    let content,trial;
    if(page==='request_sets')content=requestSetsPage(sets);
    else if(page==='trials')content=trialsPage(trials,sets);
    else if(page==='windows')content=windowsPage(windows,sets,nodes);
    else if(page==='window'){const w=await windowsApi.detail(Number(id));content=windowDetailPage(w,sets)}
    else if(page==='evidence'){trial=id?await api('/trials/'+id):trials[0];content=evidencePage(trial)}
    else if(page==='audit'){const rows=await api('/audit');content=table(['时间','操作人','动作'],rows.map(x=>[date(x.created_at),esc(x.actor),esc(x.action)]))}
    else content=nodesPage(nodes);
    const [title,subtitle]=titles[page]||titles.nodes;
    app.innerHTML=frame(page,title,subtitle,content);
    document.querySelector('#logout').onclick=()=>{session.clear();render()};
    bindForm('#node-form','/nodes',x=>({...x,position:Number(x.position),delay_ms:Number(x.delay_ms),read_capacity:Number(x.read_capacity)}),render);
    bindForm('#request-form','/request_sets',x=>({...x,events:JSON.parse(x.events)}),render);
    bindForm('#trial-form','/trials',x=>({...x,request_set_id:Number(x.request_set_id),max_delay_ms:Number(x.max_delay_ms),sticky_ms:Number(x.sticky_ms)}),async x=>{location.hash='evidence/'+x.id;await render()});
    document.querySelectorAll('.edit-node').forEach(b=>b.onclick=()=>{const n=nodes.find(x=>x.id===Number(b.dataset.id)),form=document.querySelector('#node-form');for(const key of ['id','name','role','status','lineage','position','delay_ms','read_capacity','note'])form.elements[key].value=n[key];document.querySelector('#node-heading').textContent='修改节点 #'+n.id;form.scrollIntoView({behavior:'smooth',block:'center'})});
    document.querySelectorAll('.edit-request').forEach(b=>b.onclick=()=>{const s=sets.find(x=>x.id===Number(b.dataset.id)),form=document.querySelector('#request-form');for(const key of ['id','code','title','note'])form.elements[key].value=s[key];form.elements.events.value=JSON.stringify(s.events,null,2);document.querySelector('#request-heading').textContent='修改请求集 #'+s.id;form.scrollIntoView({behavior:'smooth',block:'center'})});
    document.querySelector('#export')?.addEventListener('click',async e=>{try{await download(e.target.dataset.id);feedback('路由JSON已下载')}catch(err){feedback(err.message,true)}});
    bindWindowActions(Number(id),page);
    if((page==='evidence'&&trial&&['queued','running'].includes(trial.status))||(page==='trials'&&trials.some(x=>['queued','running'].includes(x.status))))timer=setTimeout(render,1500);
  }catch(err){
    if(err.message.includes('登录')||err.message.includes('Not authenticated')){session.clear();await render()}
    else{if(!document.querySelector('#feedback'))app.innerHTML='<main><div id="feedback"></div><button onclick="location.reload()">重新加载</button></main>';feedback(err.message,true)}
  }
}

function bindWindowActions(windowId,page){
  // 窗口列表页的创建表单
  if(page==='windows'){
    document.querySelector('#window-form')?.addEventListener('submit',async e=>{
      e.preventDefault();
      const data=formValues(e.target);
      data.horizon_ms=Number(data.horizon_ms);data.default_hold_ms=Number(data.default_hold_ms);
      try{const w=await windowsApi.create(data);feedback('窗口已创建，可在详情页发起试验。');location.hash='window/'+w.id;await render()}
      catch(err){feedback(err.message,true)}
    });
    return;
  }
  if(page!=='window'||!windowId)return;
  // 发起试验
  document.querySelector('#trial-window-form')?.addEventListener('submit',async e=>{
    e.preventDefault();
    const data=formValues(e.target);
    data.request_set_id=Number(data.request_set_id);
    data.max_delay_ms=Number(data.max_delay_ms);data.sticky_ms=Number(data.sticky_ms);
    data.cancel_at_ms=data.cancel_at_ms===''?null:Number(data.cancel_at_ms);
    try{await windowsApi.addTrial(windowId,data);feedback('试验已加入共享窗口。');await render()}
    catch(err){feedback(err.message,true)}
  });
  // 取消试验（排队中撤回）
  document.querySelectorAll('.cancel-trial').forEach(btn=>btn.addEventListener('click',async()=>{
    const trialId=Number(btn.dataset.id);
    const raw=prompt('输入窗口时间轴上的取消时刻 cancel_at_ms（取消将撤回该试验尚未开始的排队读）');
    if(raw===null)return;
    const cancel_at_ms=Number(raw);
    if(!Number.isFinite(cancel_at_ms)||cancel_at_ms<0){feedback('取消时刻必须是非负整数',true);return}
    try{await windowsApi.cancel(windowId,trialId,cancel_at_ms);feedback('试验已取消，排队读将撤回且不占容量。');await render()}
    catch(err){feedback(err.message,true)}
  }));
  // 执行窗口
  document.querySelector('#execute-window')?.addEventListener('click',async()=>{
    const btn=document.querySelector('#execute-window');btn.disabled=true;
    try{await windowsApi.execute(windowId);feedback('窗口执行完成，证据已冻结。');await render()}
    catch(err){feedback(err.message,true);btn.disabled=false}
  });
  // 导出窗口证据
  document.querySelector('#window-export')?.addEventListener('click',async()=>{
    try{await downloadWindow(windowId);feedback('窗口 JSON 已下载')}catch(err){feedback(err.message,true)}
  });
}
window.addEventListener('hashchange',render);render();
