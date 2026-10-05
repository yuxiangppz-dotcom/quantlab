// Local real workerd replay of an ALREADY published concise report. No model/data/push.
import assert from 'node:assert/strict';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {join} from 'node:path';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
const [folder, output]=process.argv.slice(2);
if (!folder || !output) throw Error('Provide saved mobile-view folder and NEW audit output');
await mkdir(output,{recursive:false});
const metadata=JSON.parse(await readFile(join(folder,'metadata.json'),'utf8'));
const html=await readFile(join(folder,'report.html'),'utf8');
const hash=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(html))).toString('hex');
assert.equal(hash,metadata.html_sha256);
const modulePath=name=>new URL(name,import.meta.url).pathname.replace(/^\/([A-Z]:)/,'$1');
const options=convertV4MiniflareOptions({name:'scout-real-replay',modules:[
  {type:'ESModule',path:modulePath('./worker.mjs')},{type:'ESModule',path:modulePath('./auth.mjs')}],
  compatibilityDate:'2026-10-01',durableObjects:{SCOUT:{className:'ScoutStore',useSQLite:true}},
  bindings:{SCOUT_API_TOKEN:'local-replay-synthetic-token-abcdefghijklmnopqrstuvwxyz',SCHEDULE_ENABLED:'false'}});
options.resourcePersistencePath=join(output,'persist');
let mf=new Miniflare(options);
try {
  const imported=await mf.dispatchFetch('https://scout.example/api/publish',{method:'POST',
    headers:{authorization:'Bearer local-replay-synthetic-token-abcdefghijklmnopqrstuvwxyz'},
    body:JSON.stringify({import_only:true,metadata,html})});
  assert.equal(imported.status,200);
  await mf.dispose();mf=new Miniflare(options);
  const ns=await mf.getDurableObjectNamespace('SCOUT');
  const result=await ns.get(ns.idFromName('scout-v1')).fetch('https://scout.example/reports/'+metadata.run_id);
  assert.equal(await result.text(),html);
  const anonymous=await mf.dispatchFetch('https://scout.example/reports/'+metadata.run_id);
  assert.equal(anonymous.status,403);
  const proof={run_id:metadata.run_id,asof:metadata.asof_session,target:metadata.target_session,
    html_sha256:hash,restart_bytes_match:true,anonymous_http_status:anonymous.status,
    candidates:(html.match(/class='card'/g)||[]).length,real_cloud_deployment:false,
    new_model_calls:0,new_provider_calls:0,wechat_sends:0};
  await writeFile(join(output,'replay-proof.json'),JSON.stringify(proof,null,2));
  console.log(JSON.stringify(proof));
} finally {await mf.dispose();}
