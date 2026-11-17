import {api} from './client.js';
export const windowsApi={
  list:()=>api('/windows'),
  detail:id=>api('/windows/'+id),
  create:data=>api('/windows','POST',data),
  addTrial:(id,data)=>api('/windows/'+id+'/trials','POST',data),
  cancel:(id,trialId,cancel_at_ms)=>api('/windows/'+id+'/trials/'+trialId+'/cancel','POST',{cancel_at_ms}),
  execute:id=>api('/windows/'+id+'/execute','POST'),
};
