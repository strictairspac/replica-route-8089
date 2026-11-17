import {windowsApi} from '../api/windows.js';
export const windowsStore={items:[],async refresh(){this.items=await windowsApi.list();return this.items;}};
