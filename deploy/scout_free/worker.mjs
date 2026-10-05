import {digest, serviceAllowed, viewerAllowed} from './auth.mjs';
import {nextPath,passwordConfigured,sessionAllowed,sessionValue,verifyPassword} from './password.mjs';

const HASH = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;
const MAX_BYTES = 1024*1024*1024; // Logical cap, below account-wide 5GB free SQLite limit.
const MAX_SNAPSHOT = 256*1024*1024;
const headers = {
  'cache-control':'private, no-store', 'x-content-type-options':'nosniff',
  'referrer-policy':'strict-origin-when-cross-origin',
  'content-security-policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
};
const response = (value, status=200, type='application/json') =>
  new Response(type==='application/json'?JSON.stringify(value):value,
    {status, headers:{...headers,'content-type':type}});
const escape = s => String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const chinaTime = now => {
  const d = new Date(now.getTime()+8*3600000);
  return {day:d.toISOString().slice(0,10), hour:d.getUTCHours()};
};
export default {
  async fetch(request, env) {
    try {
      const path = new URL(request.url).pathname;
      if (env.AUTH_MODE==='password' && path==='/login') {
        if (!passwordConfigured(env)) return response('登录尚未配置。',503,'text/plain;charset=utf-8');
        return await env.SCOUT.get(env.SCOUT.idFromName('scout-v1')).fetch(request);
      }
      if (path.startsWith('/api/')) {
        if (!(await serviceAllowed(request,env))) return response({error:'unauthorized'},403);
      } else if (env.AUTH_MODE==='password') {
        if (!(await sessionAllowed(request,env))) return new Response(null,{status:303,headers:{
          ...headers,location:'/login?next='+encodeURIComponent(nextPath(path))}});
      } else if (!(await viewerAllowed(request,env))) {
        return response('请通过 Cloudflare Access 登录后查看报告。',403,'text/plain;charset=utf-8');
      }
      return await env.SCOUT.get(env.SCOUT.idFromName('scout-v1')).fetch(request);
    } catch { return response({error:'service_unavailable'},503); }
  }
};

export class ScoutStore {
  constructor(ctx, env) { this.ctx=ctx; this.env=env; this.kv=ctx.storage.kv; }
  now() { return new Date(); }
  async fetch(request) {
    try { return await this.route(request); }
    catch { return response({error:'invalid_or_unavailable'},400); }
  }
  async json(request, max=1100000) {
    const text=await request.text();
    if (new TextEncoder().encode(text).length>max) throw Error('body_bound');
    return JSON.parse(text);
  }
  validJob(value) {
    const active=this.kv.get('active');
    if (!active || value.day!==active.day || value.owner!==active.owner) throw Error('job_owner');
    return this.kv.get(`job:${active.day}`);
  }
  async route(request) {
    const url=new URL(request.url), path=url.pathname, method=request.method;
    if (path==='/login' && this.env.AUTH_MODE==='password') {
      if (!passwordConfigured(this.env)) return response('登录尚未配置。',503,'text/plain');
      if (method==='GET') {
        const next=nextPath(url.searchParams.get('next'));
        return new Response(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scout 手机报告登录</title><style>body{font:17px/1.7 system-ui;max-width:450px;margin:60px auto;padding:20px}input,button{font:inherit;width:100%;box-sizing:border-box;margin:12px 0;padding:12px}button{background:#172b3b;color:white;border:0;border-radius:8px}</style><h1>Scout 手机报告</h1><form method="post" action="/login"><input type="hidden" name="next" value="${escape(next)}"><label>查看密码<input type="password" name="password" required maxlength="128" autocomplete="current-password"></label><button>登录查看报告</button></form></html>`,{headers:{...headers,
          'content-type':'text/html;charset=utf-8','content-security-policy':headers['content-security-policy'].replace("form-action 'none'","form-action 'self'")}});
      }
      if (method!=='POST' || request.headers.get('origin')!==url.origin) return response('不接受此登录请求。',403,'text/plain;charset=utf-8');
      const source=request.body?.getReader();
      if (!source) return response('缺少密码。',400,'text/plain');
      const chunks=[];let size=0;
      while (true) {
        const {done,value}=await source.read();if(done)break;
        size+=value.byteLength;if(size>4096){await source.cancel();return response('登录请求过大。',413,'text/plain');}
        chunks.push(value);
      }
      const all=new Uint8Array(size);let offset=0;for(const chunk of chunks){all.set(chunk,offset);offset+=chunk.byteLength;}
      const form=new URLSearchParams(new TextDecoder().decode(all));
      const now=Date.now();
      const id=await digest(new TextEncoder().encode(request.headers.get('cf-connecting-ip')||'unknown'));
      const rate=this.kv.get('login:'+id)||{until:now+15*60000,count:0};
      if (rate.until<=now){rate.until=now+15*60000;rate.count=0;}
      if (rate.count>=5) return response('尝试过多，请15分钟后再试。',429,'text/plain;charset=utf-8');
      rate.count++;this.kv.put('login:'+id,rate);
      // Slow password work stays in DO (30s CPU limit), not the 10ms Free Worker.
      if (!(await verifyPassword(form.get('password'),this.env.VIEWER_PASSWORD_HASH))) return response('密码不正确。',401,'text/plain;charset=utf-8');
      this.kv.put('login:'+id,{until:now+15*60000,count:0});
      return new Response(null,{status:303,headers:{...headers,location:nextPath(form.get('next')),
        'set-cookie':`__Host-scout=${await sessionValue(this.env)}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=43200`}});
    }
    if (method==='POST' && path==='/api/claim') {
      const value=await this.json(request,2000), now=chinaTime(this.now());
      if (this.env.SCHEDULE_ENABLED!=='true') return response({status:'schedule_disabled'});
      if (value.day!==now.day || now.hour!==8) return response({status:'outside_morning_window'});
      if (!/^[a-f0-9]{40}$/.test(this.env.APP_COMMIT||'')) throw Error('release');
      if (value.app_commit!==this.env.APP_COMMIT) throw Error('release_changed');
      const existing=this.kv.get(`job:${value.day}`);
      if (existing) return response({status:'already_claimed',job:existing});
      if (this.kv.get('active')) return response({status:'previous_job_unresolved'},409);
      if ((this.kv.get('bytes')||0)+MAX_SNAPSHOT>MAX_BYTES) return response({status:'archive_capacity_low'},507);
      const job={day:now.day,owner:crypto.randomUUID(),status:'claimed',app_commit:value.app_commit,
        started_at:new Date().toISOString(),note:'Interrupted jobs never automatically retry.'};
      this.ctx.storage.transactionSync(()=>{this.kv.put(`job:${now.day}`,job);this.kv.put('active',job);});
      return response(job);
    }
    if (method==='GET' && path==='/api/snapshot') {
      return response({snapshot:this.kv.get('head')||null, bytes:this.kv.get('bytes')||0});
    }
    if (method==='GET' && path==='/api/state') {
      const now=chinaTime(this.now());
      return response({enabled:this.env.SCHEDULE_ENABLED==='true',app_commit:this.env.APP_COMMIT,
        day:now.day,hour:now.hour,job:this.kv.get(`job:${now.day}`)||null,
        active:!!this.kv.get('active'),bytes:this.kv.get('bytes')||0});
    }
    const blob=path.match(/^\/api\/blobs\/([a-f0-9]{64})$/);
    if (blob && method==='GET') {
      const data=this.kv.get(`blob:${blob[1]}`);
      return data===undefined?response({error:'missing'},404):response(data,200,'application/octet-stream');
    }
    if (blob && method==='PUT') {
      this.validJob({day:request.headers.get('x-scout-day'),owner:request.headers.get('x-scout-owner')});
      const bytes=await request.arrayBuffer();
      if (bytes.byteLength>131072 || await digest(bytes)!==blob[1]) throw Error('blob_hash_or_bound');
      if (this.kv.get(`blob:${blob[1]}`)===undefined) {
        const used=this.kv.get('bytes')||0;
        if (used+bytes.byteLength>MAX_BYTES) return response({error:'archive_full'},507);
        this.ctx.storage.transactionSync(()=>{
          this.kv.put(`blob:${blob[1]}`,bytes);this.kv.put('bytes',used+bytes.byteLength);
        });
      }
      return response({status:'saved'});
    }
    if (method==='POST' && path==='/api/checkpoint') {
      const value=await this.json(request);
      this.validJob(value);
      const snapshot=value.snapshot;
      if (!snapshot || !HASH.test(snapshot.sha256) || !Array.isArray(snapshot.files) ||
          snapshot.files.length>5000) throw Error('snapshot');
      let total=0;
      const names=new Set();
      for (const file of snapshot.files) {
        if (typeof file.path!=='string' || file.path.length>500 || file.path.startsWith('/') ||
            file.path.includes('\\') || file.path.split('/').some(p=>!p || p==='.' || p==='..') ||
            names.has(file.path) || !HASH.test(file.sha256) || !Number.isSafeInteger(file.size) ||
            file.size<0 || file.size>32*1024*1024 || !Array.isArray(file.chunks)) throw Error('file');
        names.add(file.path);
        for (const hash of file.chunks) {
          if (!HASH.test(hash)) throw Error('chunk');
          const data=this.kv.get(`blob:${hash}`);
          if (data===undefined) throw Error('missing_chunk');
          total+=data.byteLength;
        }
      }
      if (total>MAX_SNAPSHOT) throw Error('snapshot_budget');
      const encoded=new TextEncoder().encode(JSON.stringify(snapshot.files));
      if (await digest(encoded)!==snapshot.sha256) throw Error('snapshot_hash');
      const record={...snapshot,day:value.day,saved_at:new Date().toISOString()};
      // Every revision is retained. Head is updated only after all chunks exist.
      this.ctx.storage.transactionSync(()=>{
        this.kv.put(`snapshot:${snapshot.sha256}`,record);this.kv.put('head',record);
      });
      return response({status:'checkpoint_saved'});
    }
    if (method==='POST' && path==='/api/publish') {
      const value=await this.json(request);
      if (!value.import_only) this.validJob(value);
      else if (this.env.SCHEDULE_ENABLED==='true' || this.kv.get('active')) throw Error('import_while_active');
      const m=value.metadata;
      if (!m || !ID.test(m.run_id) || !HASH.test(m.html_sha256) || !HASH.test(m.source_report_sha256) ||
          !/^\d{4}-\d{2}-\d{2}$/.test(m.target_session) || !/^\d{4}-\d{2}-\d{2}$/.test(m.asof_session) ||
          !Number.isFinite(Date.parse(m.generated_at)) || typeof value.html!=='string' ||
          (await digest(new TextEncoder().encode(value.html)))!==m.html_sha256) throw Error('report');
      let textRecord=null;
      if (value.markdown!==undefined) {
        if (typeof value.markdown!=='string' || value.markdown.length>16000 ||
            new TextEncoder().encode(value.markdown).length>60000 || !HASH.test(value.markdown_sha256) ||
            await digest(new TextEncoder().encode(value.markdown))!==value.markdown_sha256) throw Error('report_text');
        textRecord={markdown:value.markdown,sha256:value.markdown_sha256};
        const previous=this.kv.get(`report-text:${m.run_id}`);
        if (previous && JSON.stringify(previous)!==JSON.stringify(textRecord)) throw Error('report_text_immutable');
      }
      const record={metadata:m,html:value.html};
      const existing=this.kv.get(`report:${m.run_id}`);
      if (existing && JSON.stringify(existing)!==JSON.stringify(record)) throw Error('report_immutable');
      if (!existing) {
        const size=new TextEncoder().encode(JSON.stringify(record)).length;
        const used=this.kv.get('bytes')||0;
        if (used+size>MAX_BYTES) return response({error:'archive_full'},507);
        this.ctx.storage.transactionSync(()=>{
          this.kv.put(`report:${m.run_id}`,record);this.kv.put('bytes',used+size);
        });
      }
      // A separate immutable companion preserves the original HTML record during upgrades.
      if (textRecord && !this.kv.get(`report-text:${m.run_id}`)) {
        const size=new TextEncoder().encode(JSON.stringify(textRecord)).length, used=this.kv.get('bytes')||0;
        if (used+size>MAX_BYTES) return response({error:'archive_full'},507);
        this.ctx.storage.transactionSync(()=>{
          this.kv.put(`report-text:${m.run_id}`,textRecord);this.kv.put('bytes',used+size);
        });
      }
      const latest=this.kv.get('latest');
      if (!latest || latest.generated_at<=m.generated_at) this.kv.put('latest',m);
      return response({status:'published'});
    }
    if (method==='POST' && path==='/api/finish') {
      const value=await this.json(request,3000), job=this.validJob(value);
      if (!['published','failed','non_trading_day','outside_morning_window'].includes(value.status)) throw Error('status');
      if (value.status==='published') {
        const report=this.kv.get(`report:${value.run_id}`);
        if (!report || report.metadata.target_session!==job.day) throw Error('unpublished');
      }
      const result={...job,status:value.status,run_id:value.run_id||null,finished_at:new Date().toISOString()};
      this.ctx.storage.transactionSync(()=>{this.kv.put(`job:${job.day}`,result);this.kv.delete('active');});
      return response(result);
    }
    if (method==='GET' && path==='/api/push-health') {
      // Authenticated, read-only upstream probe: no key, no send, no user-chosen URL.
      try {
        const upstream=await fetch('https://sctapi.ftqq.com/',{method:'GET',redirect:'manual',
          headers:{'user-agent':'QuantLab-Scout-Cloud/1.0'},signal:AbortSignal.timeout(10000)});
        return response({upstream_http:upstream.status,content_type:upstream.headers.get('content-type')});
      } catch (error) {return response({error_type:String(error?.name||'Error').slice(0,40)},502);}
    }
    if (method==='POST' && (path==='/api/notify' || path==='/api/notify-import' || path==='/api/push-test')) {
      const value=await this.json(request,2000);
      let key, title, message, link, runId;
      const origin=new URL(request.url).origin;
      if (path==='/api/notify-import' || path==='/api/push-test') {
        if (this.env.SCHEDULE_ENABLED==='true' || this.kv.get('active') || !ID.test(value.run_id)) throw Error('import_notification');
        const report=this.kv.get(`report:${value.run_id}`);
        runId=value.run_id;
        if (!report) throw Error('missing_report');
        if (path==='/api/push-test' && !/^[a-f0-9]{40}$/.test(this.env.APP_COMMIT||'')) throw Error('release');
        key=path==='/api/push-test'?`push:test:${this.env.APP_COMMIT}`:`push:import:${value.run_id}`;
        title=path==='/api/push-test'?'Scout 云端微信修复验证':`Scout ${report.metadata.target_session} 手机报告已上线`;
        message=`这是已保存的研究候选，行情截至 ${report.metadata.asof_session}，并非新预测。效果待前瞻观察。`;
        link=`${origin}/reports/${value.run_id}`;
      } else {
        const job=this.kv.get(`job:${value.day}`);
        if (!job || job.owner!==value.owner || !['published','failed'].includes(job.status)) throw Error('notification');
        key=`push:${job.day}`;runId=job.status==='published'?job.run_id:null;
        title=`Scout ${job.day} ${job.status==='published'?'选股报告已生成':'预测未完成'}`;
        message=job.status==='published'?'研究候选，效果待前瞻观察。':'本次未发布新候选，旧报告保留。';
        link=job.status==='published'?`${origin}/reports/${job.run_id}`:`${origin}/`;
      }
      const existing=this.kv.get(key);
      if (existing) return response(existing);
      if (!/^SCT[A-Za-z0-9]{10,200}$/.test(this.env.SERVERCHAN_SENDKEY||'')) return response({status:'not_configured'});
      const text=runId?this.kv.get(`report-text:${runId}`):null;
      const payload={title,desp:`${message}\n\n${text?text.markdown+'\n\n':''}[历史网页报告](${link})\n\n网页需查看密码；本条消息详情中的正文可直接阅读。`};
      const receipt={status:'delivery_unknown',attempted_at:new Date().toISOString()};
      this.kv.put(key,receipt); // Durable intent BEFORE network send.
      try {
        const sent=await fetch(`https://sctapi.ftqq.com/${this.env.SERVERCHAN_SENDKEY}.send`,{
          method:'POST',headers:{'content-type':'application/json','user-agent':'QuantLab-Scout-Cloud/1.0'},body:JSON.stringify(payload),
          redirect:'manual',signal:AbortSignal.timeout(20000)});
        receipt.upstream_http=sent.status;
        if (sent.ok) {
          const provider=await sent.json();
          receipt.status=provider.code===0?'provider_accepted':'rejected';
          if (Number.isInteger(provider.code)) receipt.provider_code=provider.code;
        } else if (sent.status>=300 && sent.status<500) receipt.status='rejected';
      } catch (error) {receipt.error_type=String(error?.name||'Error').slice(0,40); /* Never log URLs or credentials. */ }
      this.kv.put(key,receipt);return response(receipt);
    }
    if (method==='GET' && !path.startsWith('/api/')) {
      const reportPath=path.match(/^\/reports\/([A-Za-z0-9][A-Za-z0-9_-]{0,79})$/);
      if (reportPath) {
        const report=this.kv.get(`report:${reportPath[1]}`);
        return report?response(report.html,200,'text/html;charset=utf-8'):response('报告不存在',404,'text/plain');
      }
      if (path!=='/') return response('Not found',404,'text/plain');
      const latest=this.kv.get('latest');
      const records=[...this.kv.list({prefix:'report:',limit:1000}).values()]
        .map(r=>r.metadata).sort((a,b)=>b.generated_at.localeCompare(a.generated_at));
      const body=records.map(r=>`<li><a href="/reports/${escape(r.run_id)}">${escape(r.target_session)} · 行情截至 ${escape(r.asof_session)}</a></li>`).join('');
      return response(`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Scout 手机报告</title><style>body{font:17px/1.8 system-ui;margin:24px auto;padding:0 16px;max-width:800px}li{margin:18px 0}a{color:#155b85}</style><h1>Scout 选股报告</h1><p>${latest?'研究候选，效果待前瞻观察。':'尚无已保存报告。'}</p><ul>${body}</ul></html>`,200,'text/html;charset=utf-8');
    }
    return response({error:'not_found'},404);
  }
}
