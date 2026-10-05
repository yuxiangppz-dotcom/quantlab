import assert from 'node:assert/strict';
import {test} from 'node:test';
import {pbkdf2Sync} from 'node:crypto';
import {ScoutStore} from './worker.mjs';
import {sessionAllowed,nextPath} from './password.mjs';
const password='synthetic-password-only-abcdefghijklmnopqrstuvwxyz';
const salt='01'.repeat(16), hash=pbkdf2Sync(password,Buffer.from(salt,'hex'),300000,32,'sha256').toString('hex');
const env={AUTH_MODE:'password',VIEWER_PASSWORD_HASH:`pbkdf2_sha256$300000$${salt}$${hash}`,SESSION_SECRET:'session-secret-synthetic-abcdefghijklmnopqrstuvwxyz'};
test('password login: PBKDF2 parity, signed cookie, CSRF, rate and redirect boundaries',async()=>{
  const data=new Map(); const kv={get:k=>data.get(k),put:(k,v)=>data.set(k,structuredClone(v))};
  const store=new ScoutStore({storage:{kv}},env);
  const request=(value,origin='https://scout.example')=>new Request('https://scout.example/login',{
    method:'POST',headers:{origin,'cf-connecting-ip':'192.0.2.1'},body:new URLSearchParams({password:value,next:'/reports/saved-run'})});
  assert.equal((await store.fetch(request(password,'https://attacker.example'))).status,403);
  const login=await store.fetch(request(password));
  assert.equal(login.status,303);assert.equal(login.headers.get('location'),'/reports/saved-run');
  const cookie=login.headers.get('set-cookie');
  assert.match(cookie,/Secure; HttpOnly; SameSite=Lax/);
  assert.equal(await sessionAllowed(new Request('https://scout.example/',{headers:{cookie}}),env),true);
  assert.equal(await sessionAllowed(new Request('https://scout.example/',{headers:{cookie:cookie.replace(/.$/,'0').replace('__Host-scout=','__Host-scout=0')}}),env),false);
  assert.equal(await sessionAllowed(new Request('https://scout.example/'),env),false);
  assert.equal(nextPath('https://attacker.example'),'/');assert.equal(nextPath('//attacker.example'),'/');
  for(let i=0;i<5;i++)assert.equal((await store.fetch(request('wrong-password-123'))).status,401);
  assert.equal((await store.fetch(request(password))).status,429);
  assert.equal((await store.fetch(new Request('https://scout.example/login',{method:'POST',headers:{origin:'https://scout.example'},body:'x'.repeat(4100)}))).status,413);
});
