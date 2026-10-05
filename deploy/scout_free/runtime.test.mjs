import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';

test('real workerd SQLite survives restart, rejects anonymous access and renders saved report',async()=>{
  const path=await mkdtemp(join(tmpdir(),'scout-workerd-'));
  const modulePath=name=>new URL(name,import.meta.url).pathname.replace(/^\/([A-Z]:)/,'$1');
  const options=convertV4MiniflareOptions({name:'scout',modules:[{type:'ESModule',path:modulePath('./worker.mjs')},
    {type:'ESModule',path:modulePath('./auth.mjs')}],
    compatibilityDate:'2026-10-01',durableObjects:{SCOUT:{className:'ScoutStore',useSQLite:true}},
    durableObjectsPersist:path,bindings:{SCOUT_API_TOKEN:'synthetic-token-abcdefghijklmnopqrstuvwxyz',SCHEDULE_ENABLED:'false'}});
  options.resourcePersistencePath=path;
  let mf=new Miniflare(options);
  const request=(url,init={})=>mf.dispatchFetch('https://scout.example'+url,{...init,
    headers:{authorization:'Bearer synthetic-token-abcdefghijklmnopqrstuvwxyz',...init.headers}});
  try {
    assert.equal((await mf.dispatchFetch('https://scout.example/')).status,403);
    assert.equal((await mf.dispatchFetch('https://scout.example/api/state')).status,403);
    const html='<html><h1>synthetic restart proof</h1></html>';
    const bytes=new TextEncoder().encode(html);
    const hash=Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
    const value={import_only:true,html,metadata:{run_id:'restart-fixture',generated_at:'2026-10-05T18:00:00+08:00',
      target_session:'2026-10-08',asof_session:'2026-09-30',source_report_sha256:'a'.repeat(64),html_sha256:hash}};
    assert.equal((await request('/api/publish',{method:'POST',body:JSON.stringify(value)})).status,200);
    await mf.dispose(); mf=new Miniflare(options);
    const ns=await mf.getDurableObjectNamespace('SCOUT');
    const report=await ns.get(ns.idFromName('scout-v1')).fetch('https://scout.example/reports/restart-fixture');
    assert.equal(await report.text(),html);
    assert.equal((await mf.dispatchFetch('https://scout.example/reports/restart-fixture')).status,403);
    assert.equal((await request('/api/publish',{method:'POST',body:JSON.stringify({...value,html:'tampered'})})).status,400);
  } finally {
    await mf.dispose();
    assert.equal(dirname(resolve(path)),resolve(tmpdir()));
    await rm(path,{recursive:true,force:true});
  }
});
