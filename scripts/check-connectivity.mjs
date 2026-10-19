// Phase 1 connectivity check. Read-only: lists devices, sends no commands.
// Usage: SMARTTHINGS_TOKEN=... LG_PAT=... node connectivity/check.mjs [smartthings|lg]
import { randomUUID } from 'node:crypto';

const which = process.argv[2];

async function smartthings() {
  const token = process.env.SMARTTHINGS_TOKEN;
  if (!token) return console.log('SmartThings: SKIP (set SMARTTHINGS_TOKEN)');
  const res = await fetch('https://api.smartthings.com/v1/devices', {
    headers: { Authorization: `Bearer ${token}` },
  });
  console.log(`SmartThings: HTTP ${res.status}`);
  if (!res.ok) return console.log(await res.text());
  const { items } = await res.json();
  console.log(`  ${items.length} devices`);
  for (const d of items) console.log(`  - ${d.label ?? d.name} (${d.deviceId})`);
}

async function lg() {
  const pat = process.env.LG_PAT;
  if (!pat) return console.log('LG ThinQ: SKIP (set LG_PAT)');
  const country = process.env.LG_COUNTRY ?? 'US';
  const region = process.env.LG_REGION ?? 'aic'; // US -> aic, EU -> eic, KR/JP/Asia -> kic
  const res = await fetch(`https://api-${region}.lgthinq.com/devices`, {
    headers: {
      Authorization: `Bearer ${pat}`,
      'x-country': country,
      'x-message-id': Buffer.from(randomUUID().replaceAll('-', ''), 'hex').toString('base64url').slice(0, 22),
      'x-client-id': `thinq-open-${randomUUID()}`,
      // Public key published in LG's own pythinqconnect SDK (thinqconnect/const.py)
      'x-api-key': 'v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3',
      'x-service-phase': 'OP',
    },
  });
  console.log(`LG ThinQ: HTTP ${res.status}`);
  if (!res.ok) return console.log(await res.text());
  const { response } = await res.json();
  console.log(`  ${response.length} devices`);
  for (const d of response) console.log(`  - ${d.deviceInfo?.alias ?? '?'} [${d.deviceInfo?.deviceType}] (${d.deviceId})`);
}

if (!which || which === 'smartthings') await smartthings();
if (!which || which === 'lg') await lg();
