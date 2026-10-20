// Sign-in with the Cognito hosted UI: authorization code + PKCE, no client secret. The CSP (script-src 'self')
// is what keeps these tokens out of reach of injected scripts.

export interface Config {
  apiUrl: string;
  authUrl: string;
  clientId: string;
  region: string;
  budgetUsd?: number;
}

interface Tokens {
  accessToken: string;
  refreshToken?: string;
  expiresAt: number; // ms since epoch
}

const TOKENS = "costwatcher.tokens";
const PKCE = "costwatcher.pkce";

function read(store: Storage, key: string): string | null {
  try {
    return store.getItem(key);
  } catch {
    return null;
  }
}
function write(store: Storage, key: string, value: string | null): void {
  try {
    if (value === null) store.removeItem(key);
    else store.setItem(key, value);
  } catch {
    // Storage can be blocked (private windows); the session then lasts until the page closes.
  }
}

let memory: Tokens | null = null;

function saveTokens(tokens: Tokens | null): void {
  memory = tokens;
  write(localStorage, TOKENS, tokens && JSON.stringify(tokens));
}
function loadTokens(): Tokens | null {
  if (memory) return memory;
  const raw = read(localStorage, TOKENS);
  if (!raw) return null;
  try {
    memory = JSON.parse(raw) as Tokens;
    return memory;
  } catch {
    return null;
  }
}

export async function loadConfig(): Promise<Config> {
  const response = await fetch("/config.json", { cache: "no-store" });
  const type = response.headers.get("content-type") ?? "";
  // CloudFront answers a missing file with index.html, so check what came back.
  if (!response.ok || !type.includes("json")) throw new Error("config.json is missing; deploy the stack or copy it into site/public");
  return (await response.json()) as Config;
}

const redirectUri = () => `${window.location.origin}/`;

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(48)));
}

export async function signIn(config: Config): Promise<void> {
  const verifier = randomString();
  const state = randomString();
  write(sessionStorage, PKCE, JSON.stringify({ verifier, state }));
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const query = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: redirectUri(),
    scope: "openid email",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  window.location.assign(`${config.authUrl}/oauth2/authorize?${query}`);
}

export function signOut(config: Config): void {
  saveTokens(null);
  const query = new URLSearchParams({ client_id: config.clientId, logout_uri: redirectUri() });
  window.location.assign(`${config.authUrl}/logout?${query}`);
}

async function tokenRequest(config: Config, body: Record<string, string>): Promise<Tokens> {
  const response = await fetch(`${config.authUrl}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: config.clientId, ...body }),
  });
  if (!response.ok) throw new Error(`sign-in failed (${response.status})`);
  const json = (await response.json()) as { access_token: string; refresh_token?: string; expires_in: number };
  return {
    accessToken: json.access_token,
    refreshToken: json.refresh_token ?? loadTokens()?.refreshToken, // a refresh does not return a new refresh token
    expiresAt: Date.now() + json.expires_in * 1000,
  };
}

/** Finishes a sign-in when the page was opened by the hosted UI's redirect (?code=...&state=...). */
export async function handleRedirect(config: Config): Promise<void> {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code && !params.has("error")) return;
  const saved = JSON.parse(read(sessionStorage, PKCE) ?? "null") as { verifier: string; state: string } | null;
  write(sessionStorage, PKCE, null);
  window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  if (!code || !saved || params.get("state") !== saved.state) throw new Error("sign-in was not completed; try again");
  saveTokens(
    await tokenRequest(config, {
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(),
      code_verifier: saved.verifier,
    }),
  );
}

/** A usable access token, refreshed a minute early, or null when the person has to sign in. */
export async function accessToken(config: Config): Promise<string | null> {
  const tokens = loadTokens();
  if (!tokens) return null;
  if (tokens.expiresAt - 60_000 > Date.now()) return tokens.accessToken;
  if (!tokens.refreshToken) return null;
  try {
    const fresh = await tokenRequest(config, { grant_type: "refresh_token", refresh_token: tokens.refreshToken });
    saveTokens(fresh);
    return fresh.accessToken;
  } catch {
    saveTokens(null);
    return null;
  }
}

export function forgetTokens(): void {
  saveTokens(null);
}
