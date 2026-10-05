import {digest, serviceAllowed, viewerAllowed} from './auth.mjs';

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
      if (path.startsWith('/api/')) {
        if (!(await serviceAllowed(request,env))) return response({error:'unauthorized'},403);
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
    if (method==='POST' && path==='/api/notify') {
      const value=await this.json(request,2000), job=this.kv.get(`job:${value.day}`);
      if (!job || job.owner!==value.owner || !['published','failed'].includes(job.status)) throw Error('notification');
      const existing=this.kv.get(`push:${job.day}`);
      if (existing) return response(existing);
      if (!/^SCT[A-Za-z0-9]{10,200}$/.test(this.env.SERVERCHAN_SENDKEY||'')) return response({status:'not_configured'});
      const origin=new URL(request.url).origin;
      const link=job.status==='published'?`${origin}/reports/${job.run_id}`:`${origin}/`;
      const payload={title:`Scout ${job.day} ${job.status==='published'?'选股报告已生成':'预测未完成'}`,
        desp:`${job.status==='published'?'研究候选，效果待前瞻观察。':'本次未发布新候选，旧报告保留。'}\n\n[查看手机报告](${link})\n\n需要登录。`};
      const receipt={status:'delivery_unknown',attempted_at:new Date().toISOString()};
      this.kv.put(`push:${job.day}`,receipt); // Durable intent BEFORE network send.
      try {
        const sent=await fetch(`https://sctapi.ftqq.com/${this.env.SERVERCHAN_SENDKEY}.send`,{
          method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload),
          redirect:'error',signal:AbortSignal.timeout(20000)});
        if (sent.ok) receipt.status=(await sent.json()).code===0?'provider_accepted':'rejected';
        else if (sent.status>=400 && sent.status<500) receipt.status='rejected';
      } catch { /* Delivery remains unknown; no automatic resend. */ }
      this.kv.put(`push:${job.day}`,receipt);return response(receipt);
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
