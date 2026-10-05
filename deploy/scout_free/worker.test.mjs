import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ScoutStore} from './worker.mjs';
import {digest, serviceAllowed, viewerAllowed} from './auth.mjs';

const token='synthetic-test-token-abcdefghijklmnopqrstuvwxyz';
const env={SCOUT_API_TOKEN:token,SCHEDULE_ENABLED:'false',APP_COMMIT:'a'.repeat(40)};
const encoder=new TextEncoder();
function context() {
  const data=new Map();
  const kv={get:k=>data.get(k),put:(k,v)=>data.set(k,structuredClone(v)),delete:k=>data.delete(k),
    list:({prefix,limit})=>new Map([...data].filter(([k])=>k.startsWith(prefix)).slice(0,limit))};
  return {storage:{kv,transactionSync:fn=>fn()},data};
}
const req=(path,value,method='POST',extra={})=>new Request('https://scout.example'+path,
  {method,headers:extra,body:value===undefined?undefined:JSON.stringify(value)});
async function report() {
  const html='<html><meta charset="utf-8"><h1>真实样本在单独验收中，此处为合成测试</h1></html>';
  return {import_only:true,html,metadata:{run_id:'synthetic-report',generated_at:'2026-10-05T18:00:00+08:00',
    target_session:'2026-10-08',asof_session:'2026-09-30',source_report_sha256:'b'.repeat(64),
    html_sha256:await digest(encoder.encode(html))}};
}

test('API bearer and Access viewer are separate, fail closed',async()=>{
  assert.equal(await serviceAllowed(req('/api/state',undefined,'GET'),env),false);
  assert.equal(await serviceAllowed(req('/api/state',undefined,'GET',{authorization:`Bearer ${token}`}),env),true);
  assert.equal(await viewerAllowed(req('/',undefined,'GET',{authorization:`Bearer ${token}`}),env),false);
  assert.equal(await viewerAllowed(req('/',undefined,'GET',{'cf-access-jwt-assertion':'fake'}),env),false);
});

test('Access checks real RSA signature, owner, issuer, audience and expiry',async()=>{
  const pair=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,
    publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
  const jwk=await crypto.subtle.exportKey('jwk',pair.publicKey); jwk.kid='test-key';
  const options={ACCESS_ISSUER:'https://scout-test.cloudflareaccess.com',ACCESS_AUD:'aud',OWNER_EMAIL:'owner@example.com'};
  const now=Math.floor(Date.now()/1000);
  const claims={iss:options.ACCESS_ISSUER,aud:['aud'],email:options.OWNER_EMAIL,iat:now,exp:now+60};
  const b64=b=>Buffer.from(b).toString('base64url');
  async function signed(c) {
    const unsigned=b64(JSON.stringify({alg:'RS256',kid:'test-key'}))+'.'+b64(JSON.stringify(c));
    return unsigned+'.'+b64(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',pair.privateKey,encoder.encode(unsigned)));
  }
  const keys=async()=>Response.json({keys:[jwk]});
  async function allowed(c) {
    return viewerAllowed(req('/',undefined,'GET',{'cf-access-jwt-assertion':await signed(c)}),options,keys);
  }
  assert.equal(await allowed(claims),true);
  for(const patch of [{aud:['other']},{email:'other@example.com'},{iss:'https://attacker.example'},{exp:now-1},{iat:now+100}])
    assert.equal(await allowed({...claims,...patch}),false);
  const signature=await signed(claims);
  assert.equal(await viewerAllowed(req('/',undefined,'GET',{'cf-access-jwt-assertion':signature.slice(0,-5)+'abcde'}),options,keys),false);
});

test('disabled schedule and report integrity, immutability, POST-only operations',async()=>{
  const ctx=context(), store=new ScoutStore(ctx,env);
  assert.equal((await (await store.fetch(req('/api/claim',{day:'2026-10-08',app_commit:'a'.repeat(40)}))).json()).status,'schedule_disabled');
  assert.equal(ctx.data.has('active'),false);
  const saved=await report();
  assert.equal((await store.fetch(req('/api/publish',saved))).status,200);
  assert.equal((await store.fetch(req('/api/publish',saved))).status,200);
  assert.equal((await store.fetch(req('/api/publish',{...saved,html:'altered'}))).status,400);
  assert.equal((await store.fetch(req('/api/claim',undefined,'GET'))).status,404);
  assert.equal((await store.fetch(req('/reports/synthetic-report',undefined,'GET'))).status,200);
  assert.equal((await store.fetch(req('/runtime/secret',undefined,'GET'))).status,404);
});

test('atomic same-day claim and unresolved previous day suppress repeat paid work',async()=>{
  const ctx=context(), store=new ScoutStore(ctx,{...env,SCHEDULE_ENABLED:'true'});
  store.now=()=>new Date('2026-10-08T00:03:00Z');
  const input={day:'2026-10-08',app_commit:'a'.repeat(40)};
  const first=await (await store.fetch(req('/api/claim',input))).json();
  assert.equal(first.status,'claimed');
  const duplicate=await (await store.fetch(req('/api/claim',input))).json();
  assert.equal(duplicate.status,'already_claimed');
  assert.equal(duplicate.job.owner,first.owner);
  store.now=()=>new Date('2026-10-09T00:03:00Z');
  assert.equal((await store.fetch(req('/api/claim',{...input,day:'2026-10-09'}))).status,409);
  store.now=()=>new Date('2026-10-09T01:00:00Z');
  assert.equal((await (await store.fetch(req('/api/claim',{...input,day:'2026-10-09'}))).json()).status,'outside_morning_window');
});

test('chunks need durable owner, bad hashes and incomplete checkpoints rejected',async()=>{
  const ctx=context(), store=new ScoutStore(ctx,env);
  const chunk=encoder.encode('fixture bytes'), hash=await digest(chunk);
  const upload=new Request(`https://scout.example/api/blobs/${hash}`,{method:'PUT',body:chunk});
  assert.equal((await store.fetch(upload)).status,400);
  const job={day:'2026-10-08',owner:'uuid',status:'claimed'};
  ctx.data.set('active',job);ctx.data.set('job:'+job.day,job);
  const upload2=new Request(`https://scout.example/api/blobs/${hash}`,{method:'PUT',body:chunk,
    headers:{'x-scout-day':job.day,'x-scout-owner':job.owner}});
  assert.equal((await store.fetch(upload2)).status,200);
  const files=[{path:'runs/file.json',sha256:hash,size:chunk.length,chunks:['c'.repeat(64)]}];
  assert.equal((await store.fetch(req('/api/checkpoint',{...job,snapshot:{files,sha256:await digest(encoder.encode(JSON.stringify(files)))}}))).status,400);
  assert.equal(ctx.data.has('head'),false);
  assert.equal((await store.fetch(req('/api/finish',{...job,status:'published',run_id:'missing'}))).status,400);
  assert.equal(ctx.data.has('active'),true);
});

test('finish does not erase previous failures; notification intents survive unknown send',async()=>{
  const ctx=context(), store=new ScoutStore(ctx,{...env,SERVERCHAN_SENDKEY:'SCTsynthetic123456789'});
  const job={day:'2026-10-08',owner:'uuid',status:'claimed'};
  ctx.data.set('active',job);ctx.data.set('job:'+job.day,job);
  assert.equal((await store.fetch(req('/api/finish',{...job,status:'failed'}))).status,200);
  assert.equal(ctx.data.has('active'),false);
  const original=globalThis.fetch;
  let calls=0;
  globalThis.fetch=async()=>{calls++;assert.equal(ctx.data.get('push:'+job.day).status,'delivery_unknown');throw Error('synthetic timeout');};
  try {
    const first=await (await store.fetch(req('/api/notify',job))).json();
    const second=await (await store.fetch(req('/api/notify',job))).json();
    assert.equal(first.status,'delivery_unknown');assert.deepEqual(second,first);assert.equal(calls,1);
    assert.equal(ctx.data.get('job:'+job.day).status,'failed');
  } finally { globalThis.fetch=original; }
});


test('saved-report link delivery is durable, restricted and never reclassifies an import as a forecast',async()=>{
  const ctx=context(), store=new ScoutStore(ctx,{...env,SERVERCHAN_SENDKEY:'SCTsynthetic123456789'});
  const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async(url,options)=>{
    calls++;const payload=JSON.parse(options.body);
    assert.match(payload.desp,/并非新预测/);
    assert.match(payload.desp,/https:\/\/scout.example\/reports\/synthetic-report/);
    assert.equal(ctx.data.get('push:import:synthetic-report').status,'delivery_unknown');
    return Response.json({code:0});
  };
  try {
    assert.equal((await store.fetch(req('/api/notify-import',{run_id:'missing'}))).status,400);
    await store.fetch(req('/api/publish',await report()));
    const value={run_id:'synthetic-report'};
    const first=await (await store.fetch(req('/api/notify-import',value))).json();
    assert.equal(first.status,'provider_accepted');
    assert.deepEqual(await (await store.fetch(req('/api/notify-import',value))).json(),first);
    assert.equal(calls,1);
    store.env={...store.env,SCHEDULE_ENABLED:'true'};
    assert.equal((await store.fetch(req('/api/notify-import',value))).status,400);
    assert.equal(calls,1);assert.equal(ctx.data.has('job:2026-10-08'),false);
  } finally {globalThis.fetch=original;}
});


test('deployment repair push is bounded per release, preserves unknown original send and records safe diagnostics',async()=>{
  const ctx=context(),store=new ScoutStore(ctx,{...env,SERVERCHAN_SENDKEY:'SCTsynthetic123456789'});
  await store.fetch(req('/api/publish',await report()));
  ctx.data.set('push:import:synthetic-report',{status:'delivery_unknown'});
  const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async(url,options)=>{
    assert.equal(options.headers['user-agent'],'QuantLab-Scout-Cloud/1.0');
    calls++;return Response.json({code:0});
  };
  try {
    const value={run_id:'synthetic-report'};
    const first=await (await store.fetch(req('/api/push-test',value))).json();
    assert.equal(first.status,'provider_accepted');assert.equal(first.upstream_http,200);
    assert.deepEqual(await (await store.fetch(req('/api/push-test',value))).json(),first);
    assert.equal(calls,1);assert.equal(ctx.data.get('push:import:synthetic-report').status,'delivery_unknown');
    store.env={...store.env,SCHEDULE_ENABLED:'true'};
    assert.equal((await store.fetch(req('/api/push-test',value))).status,400);
    assert.equal(calls,1);
  } finally {globalThis.fetch=original;}
});
