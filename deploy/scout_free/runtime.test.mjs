import assert from 'node:assert/strict';
import {pbkdf2Sync} from 'node:crypto';
import {test} from 'node:test';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';

test('real workerd SQLite survives restart, rejects anonymous access and renders saved report',async()=>{
  const path=await mkdtemp(join(tmpdir(),'scout-workerd-'));
  const modulePath=name=>new URL(name,import.meta.url).pathname.replace(/^\/([A-Z]:)/,'$1');
  const password='synthetic-workerd-password-123456789';
  const salt='02'.repeat(16);
  const passwordHash=pbkdf2Sync(password,Buffer.from(salt,'hex'),100000,32,'sha256').toString('hex');
  const options=convertV4MiniflareOptions({name:'scout',modules:[{type:'ESModule',path:modulePath('./worker.mjs')},
    {type:'ESModule',path:modulePath('./auth.mjs')},{type:'ESModule',path:modulePath('./password.mjs')}],
    compatibilityDate:'2026-10-01',durableObjects:{SCOUT:{className:'ScoutStore',useSQLite:true}},
    durableObjectsPersist:path,bindings:{SCOUT_API_TOKEN:'synthetic-token-abcdefghijklmnopqrstuvwxyz',SCHEDULE_ENABLED:'false',AUTH_MODE:'password',SESSION_SECRET:'synthetic-runtime-session-secret-abcdefghijklmnopqrstuvwxyz',
      VIEWER_PASSWORD_HASH:`pbkdf2_sha256$100000$${salt}$${passwordHash}`}});
  options.resourcePersistencePath=path;
  let mf=new Miniflare(options);
  const request=(url,init={})=>mf.dispatchFetch('https://scout.example'+url,{...init,
    headers:{authorization:'Bearer synthetic-token-abcdefghijklmnopqrstuvwxyz',...init.headers}});
  try {
    assert.equal((await mf.dispatchFetch('https://scout.example/',{redirect:'manual'})).status,303);
    assert.equal((await mf.dispatchFetch('https://scout.example/api/state')).status,403);
    const html='<html><h1>synthetic restart proof</h1></html>';
    const bytes=new TextEncoder().encode(html);
    const hash=Buffer.from(await crypto.subtle.digest('SHA-256',bytes)).toString('hex');
    const value={import_only:true,html,metadata:{run_id:'restart-fixture',generated_at:'2026-10-05T18:00:00+08:00',
      target_session:'2026-10-08',asof_session:'2026-09-30',source_report_sha256:'a'.repeat(64),html_sha256:hash}};
    assert.equal((await request('/api/publish',{method:'POST',body:JSON.stringify(value)})).status,200);
    await mf.dispose(); mf=new Miniflare(options);
    const login=await mf.dispatchFetch('https://scout.example/login',{method:'POST',redirect:'manual',
      headers:{origin:'https://scout.example','cf-connecting-ip':'192.0.2.3'},
      body:new URLSearchParams({password,next:'/reports/restart-fixture'})});
    assert.equal(login.status,303);assert.equal(login.headers.get('location'),'/reports/restart-fixture');
    const cookie=login.headers.get('set-cookie');assert.match(cookie,/HttpOnly/);
    const report=await mf.dispatchFetch('https://scout.example/reports/restart-fixture',{headers:{cookie}});
    assert.equal(await report.text(),html);
    assert.equal((await mf.dispatchFetch('https://scout.example/reports/restart-fixture',{redirect:'manual'})).status,303);
    assert.equal((await request('/api/publish',{method:'POST',body:JSON.stringify({...value,html:'tampered'})})).status,400);
  } finally {
    await mf.dispose();
    assert.equal(dirname(resolve(path)),resolve(tmpdir()));
    await rm(path,{recursive:true,force:true});
  }
});
