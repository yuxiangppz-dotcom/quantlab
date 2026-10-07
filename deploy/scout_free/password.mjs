const enc=new TextEncoder();
const hex=bytes=>[...new Uint8Array(bytes)].map(v=>v.toString(16).padStart(2,'0')).join('');
const bytes=s=>Uint8Array.from(s.match(/../g),v=>parseInt(v,16));
const mac=async (value,key)=>{
  const secret=await crypto.subtle.importKey('raw',enc.encode(key),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  return hex(await crypto.subtle.sign('HMAC',secret,enc.encode(value)));
};
export function passwordConfigured(env) {
  return env.AUTH_MODE==='password' && /^pbkdf2_sha256\$100000\$[a-f0-9]{32}\$[a-f0-9]{64}$/.test(env.VIEWER_PASSWORD_HASH||'') &&
    typeof env.SESSION_SECRET==='string' && env.SESSION_SECRET.length>=48;
}
export async function verifyPassword(value, hash) {
  if (typeof value!=='string' || value.length<12 || value.length>128) return false;
  const [,iterations,salt,expected]=hash.split('$');
  const key=await crypto.subtle.importKey('raw',enc.encode(value),'PBKDF2',false,['deriveBits']);
  const got=hex(await crypto.subtle.deriveBits({name:'PBKDF2',salt:bytes(salt),iterations:Number(iterations),hash:'SHA-256'},key,256));
  return got===expected;
}
export async function sessionValue(env) {
  const expiry=Math.floor(Date.now()/1000)+12*3600;
  const value=`${expiry}.${crypto.randomUUID()}`;
  return value+'.'+await mac(value,env.SESSION_SECRET);
}
export async function sessionAllowed(request,env) {
  if (!passwordConfigured(env)) return false;
  try {
    const value=(request.headers.get('cookie')||'').split(';').map(v=>v.trim())
      .find(v=>v.startsWith('__Host-scout='))?.slice('__Host-scout='.length);
    if (!value || value.length>160) return false;
    const [expiry,nonce,sig,extra]=value.split('.');
    const now=Math.floor(Date.now()/1000);
    if (extra!==undefined || !/^\d{10}$/.test(expiry) || Number(expiry)<=now || Number(expiry)>now+12*3600 ||
        !/^[a-f0-9-]{36}$/.test(nonce) || !/^[a-f0-9]{64}$/.test(sig)) return false;
    return sig===await mac(`${expiry}.${nonce}`,env.SESSION_SECRET);
  } catch {return false;}
}
export const nextPath=value=>/^\/reports\/[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(value||'')?value:'/';
