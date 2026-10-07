// Fail closed even when an Access edge policy is missing or misconfigured.
const certCache = new Map();
export const digest = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
  .map(v => v.toString(16).padStart(2, '0')).join('');
const decode = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
export async function serviceAllowed(request, env) {
  if (!env.SCOUT_API_TOKEN || env.SCOUT_API_TOKEN.length < 32) return false;
  const got = request.headers.get('authorization') || '';
  return (await digest(new TextEncoder().encode(got))) ===
    (await digest(new TextEncoder().encode(`Bearer ${env.SCOUT_API_TOKEN}`)));
}
export async function viewerAllowed(request, env, fetchKeys = fetch) {
  try {
    if (!/^https:\/\/[a-z0-9-]+\.cloudflareaccess\.com$/.test(env.ACCESS_ISSUER || '') ||
        !env.ACCESS_AUD || !env.OWNER_EMAIL) return false;
    const token = request.headers.get('cf-access-jwt-assertion') || '';
    if (token.length > 12000) return false;
    const [head, body, sig, extra] = token.split('.');
    if (!head || !body || !sig || extra !== undefined) return false;
    const h = JSON.parse(new TextDecoder().decode(decode(head)));
    const claims = JSON.parse(new TextDecoder().decode(decode(body)));
    const now = Math.floor(Date.now()/1000);
    if (h.alg !== 'RS256' || typeof h.kid !== 'string' ||
        claims.iss !== env.ACCESS_ISSUER || !Array.isArray(claims.aud) ||
        !claims.aud.includes(env.ACCESS_AUD) || !Number.isFinite(claims.exp) ||
        claims.exp <= now || !Number.isFinite(claims.iat) || claims.iat > now + 30 ||
        (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > now)) ||
        claims.email?.toLowerCase() !== env.OWNER_EMAIL.toLowerCase()) return false;
    let cached = certCache.get(env.ACCESS_ISSUER);
    if (!cached || cached.until < now || !cached.keys.some(k => k.kid === h.kid)) {
      const response = await fetchKeys(`${env.ACCESS_ISSUER}/cdn-cgi/access/certs`,
        {redirect:'error', signal:AbortSignal.timeout(10000)});
      if (!response.ok) return false;
      const keys = (await response.json()).keys;
      if (!Array.isArray(keys) || keys.length > 10) return false;
      cached = {keys, until:now+300}; certCache.set(env.ACCESS_ISSUER, cached);
    }
    const jwk = cached.keys.find(k => k.kid === h.kid && k.kty === 'RSA');
    if (!jwk) return false;
    const key = await crypto.subtle.importKey('jwk', jwk,
      {name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'}, false, ['verify']);
    return await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,decode(sig),
      new TextEncoder().encode(`${head}.${body}`));
  } catch { return false; }
}
