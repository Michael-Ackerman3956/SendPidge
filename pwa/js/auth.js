// Passkey sign-in through Cognito managed login: Authorization Code + PKCE (S256), the code swapped for ID and access
// tokens, refreshed when they near expiry; the access token only lists the user's passkeys. The config is the deployed
// /auth-config.json. Tokens are never logged.
const SIGN_IN_CONFIG_PATH = "/auth-config.json";
const SIGN_IN_SESSION_STORAGE_KEY = "sendpidge.cognitoSession";
const CODE_VERIFIER_STORAGE_KEY = "sendpidge.signInCodeVerifier";
const SIGN_IN_STATE_STORAGE_KEY = "sendpidge.signInState";
const RANDOM_VALUE_BYTE_LENGTH = 32;
const ID_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60;
const OAUTH_SCOPE = "openid email aws.cognito.signin.user.admin"; // the last lets the access token list the user's passkeys
const LIST_PASSKEYS_TARGET = "AWSCognitoIdentityProviderService.ListWebAuthnCredentials";
// Query parameters managed login appends to the callback URL; all are stripped once read.
const REDIRECT_QUERY_PARAMETERS = ["code", "state", "error", "error_description"];
const SIGN_IN_OFF = Object.freeze({ isSignInEnabled: false });

let sessionForThisVisit = null; // the session, also kept here when localStorage refuses it

// A failure whose message names no token; any other error is logged by its name only.
class SignInFailure extends Error {}
// The token endpoint refused the grant (400: an invalid, expired or revoked code or refresh token); retrying cannot help.
class TokenGrantRejected extends SignInFailure {}
const TOKEN_GRANT_REJECTED_STATUS = 400;

function describeFailure(error) {
  return error instanceof SignInFailure ? error.message : error.name;
}

function requireFilledText(value, fieldName) {
  if (typeof value !== "string" || value.length === 0) throw new SignInFailure(`${fieldName} missing`);
  return value;
}

// The sign-in config, or sign-in off (the server still refuses a linked user's link, so a failure here is fail-closed).
export async function readSignInConfig() {
  try {
    const response = await fetch(SIGN_IN_CONFIG_PATH, { cache: "no-store" });
    if (!response.ok) throw new SignInFailure(`auth-config.json answered ${response.status}`);
    const config = await response.json();
    if (config.isSignInEnabled !== true) return SIGN_IN_OFF;
    return Object.freeze({
      isSignInEnabled: true,
      hostedUiBaseUrl: requireFilledText(config.hostedUiBaseUrl, "hostedUiBaseUrl"),
      appClientId: requireFilledText(config.appClientId, "appClientId"),
      redirectUrl: requireFilledText(config.redirectUrl, "redirectUrl"),
      identityProviderUrl: requireFilledText(config.identityProviderUrl, "identityProviderUrl"),
    });
  } catch (configError) {
    console.error("SendPidge: sign-in config unavailable; sign-in is off", describeFailure(configError));
    return SIGN_IN_OFF;
  }
}

function encodeBase64Url(bytes) {
  let binaryText = "";
  bytes.forEach((byte) => { binaryText += String.fromCharCode(byte); });
  return btoa(binaryText).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function createRandomBase64UrlValue() {
  return encodeBase64Url(crypto.getRandomValues(new Uint8Array(RANDOM_VALUE_BYTE_LENGTH)));
}

async function computeCodeChallenge(codeVerifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(codeVerifier));
  return encodeBase64Url(new Uint8Array(digest));
}

// Sends the browser to managed login; the PKCE verifier and the state wait in sessionStorage for the redirect back.
export async function startSignIn(signInConfig) {
  const codeVerifier = createRandomBase64UrlValue();
  const signInState = createRandomBase64UrlValue();
  try {
    window.sessionStorage.setItem(CODE_VERIFIER_STORAGE_KEY, codeVerifier);
    window.sessionStorage.setItem(SIGN_IN_STATE_STORAGE_KEY, signInState);
  } catch (storageError) {
    console.error("SendPidge: sign-in not started; session storage is unavailable", storageError.name);
    return;
  }
  const authorizeUrl = new URL(`${signInConfig.hostedUiBaseUrl}/oauth2/authorize`);
  authorizeUrl.search = new URLSearchParams({
    response_type: "code",
    client_id: signInConfig.appClientId,
    redirect_uri: signInConfig.redirectUrl,
    scope: OAUTH_SCOPE,
    state: signInState,
    code_challenge: await computeCodeChallenge(codeVerifier),
    code_challenge_method: "S256",
  }).toString();
  window.location.assign(authorizeUrl.toString());
}

// Reads and removes the verifier and the state: each sign-in attempt uses them once.
function takeStoredSignInAttempt() {
  try {
    const signInAttempt = {
      codeVerifier: window.sessionStorage.getItem(CODE_VERIFIER_STORAGE_KEY),
      expectedState: window.sessionStorage.getItem(SIGN_IN_STATE_STORAGE_KEY),
    };
    window.sessionStorage.removeItem(CODE_VERIFIER_STORAGE_KEY);
    window.sessionStorage.removeItem(SIGN_IN_STATE_STORAGE_KEY);
    return signInAttempt;
  } catch (storageError) {
    console.error("SendPidge: could not read the sign-in attempt", storageError.name);
    return { codeVerifier: null, expectedState: null };
  }
}

function stripRedirectQueryFromAddressBar(pageUrl) {
  REDIRECT_QUERY_PARAMETERS.forEach((parameterName) => pageUrl.searchParams.delete(parameterName));
  window.history.replaceState(null, "", pageUrl.pathname + pageUrl.search + pageUrl.hash);
}

// POSTs a form to the token endpoint; throws on any HTTP failure or a response without an id_token or access_token.
async function requestTokens(signInConfig, formFields) {
  const response = await fetch(`${signInConfig.hostedUiBaseUrl}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(formFields).toString(),
    cache: "no-store",
  });
  if (response.status === TOKEN_GRANT_REJECTED_STATUS) throw new TokenGrantRejected("token endpoint refused the grant");
  if (!response.ok) throw new SignInFailure(`token endpoint answered ${response.status}`);
  const tokenResponse = await response.json();
  requireFilledText(tokenResponse.id_token, "id_token");
  requireFilledText(tokenResponse.access_token, "access_token");
  return tokenResponse;
}

// After managed login (?code=&state=): the state must match the stored one, then the code becomes a stored session.
// The redirect parameters leave the address bar either way. Returns true when a session was stored.
export async function completeSignInFromRedirect(signInConfig) {
  const pageUrl = new URL(window.location.href);
  const authorizationCode = pageUrl.searchParams.get("code");
  const returnedState = pageUrl.searchParams.get("state");
  if (authorizationCode === null && returnedState === null) return false;
  stripRedirectQueryFromAddressBar(pageUrl);
  const { codeVerifier, expectedState } = takeStoredSignInAttempt();
  if (authorizationCode === null || codeVerifier === null || expectedState === null || returnedState !== expectedState) {
    console.error("SendPidge: sign-in redirect refused: no code, or the state does not match this browser's attempt");
    return false;
  }
  try {
    const tokenResponse = await requestTokens(signInConfig, {
      grant_type: "authorization_code",
      client_id: signInConfig.appClientId,
      code: authorizationCode,
      redirect_uri: signInConfig.redirectUrl,
      code_verifier: codeVerifier,
    });
    saveSignInSession({
      idToken: tokenResponse.id_token,
      accessToken: tokenResponse.access_token,
      refreshToken: requireFilledText(tokenResponse.refresh_token, "refresh_token"),
    });
    return true;
  } catch (tokenError) {
    console.error("SendPidge: sign-in code exchange failed", describeFailure(tokenError));
    return false;
  }
}

function saveSignInSession(session) {
  sessionForThisVisit = session;
  try {
    window.localStorage.setItem(SIGN_IN_SESSION_STORAGE_KEY, JSON.stringify(session));
  } catch (storageError) {
    console.error("SendPidge: could not save the sign-in session; it lasts for this visit only", storageError.name);
  }
}

function readSignInSession() {
  if (sessionForThisVisit !== null) return sessionForThisVisit;
  try {
    const storedText = window.localStorage.getItem(SIGN_IN_SESSION_STORAGE_KEY);
    if (storedText === null) return null;
    const storedSession = JSON.parse(storedText);
    if (typeof storedSession?.idToken !== "string" || typeof storedSession?.accessToken !== "string"
      || typeof storedSession?.refreshToken !== "string") {
      throw new SignInFailure("stored sign-in session is incomplete");
    }
    sessionForThisVisit = storedSession;
    return storedSession;
  } catch (storageError) {
    console.error("SendPidge: could not read the sign-in session", describeFailure(storageError));
    return null;
  }
}

function clearSignInSession() {
  sessionForThisVisit = null;
  try {
    window.localStorage.removeItem(SIGN_IN_SESSION_STORAGE_KEY);
  } catch (storageError) {
    console.error("SendPidge: could not clear the sign-in session", storageError.name);
  }
}

export function hasSignInSession() {
  return readSignInSession() !== null;
}

// The ID token's exp, read only to schedule a refresh: the server verifies the token, never this. null when unreadable.
function readIdTokenExpirySeconds(idToken) {
  try {
    const payloadSegment = idToken.split(".")[1];
    const base64Text = payloadSegment.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payloadSegment.length / 4) * 4, "=");
    const payload = JSON.parse(atob(base64Text));
    return typeof payload.exp === "number" ? payload.exp : null;
  } catch (decodeError) {
    console.error("SendPidge: could not read the ID token expiry", decodeError.name);
    return null;
  }
}

function isIdTokenExpiringSoon(idToken) {
  const expirySeconds = readIdTokenExpirySeconds(idToken);
  return expirySeconds === null || expirySeconds - Date.now() / 1000 < ID_TOKEN_REFRESH_MARGIN_SECONDS;
}

// The stored ID token, refreshed first when it expires within 5 minutes. null when there is no session or the endpoint
// refuses the refresh token (the session is then cleared, so the user signs in again). Offline, a 5xx or a malformed
// answer keeps the session and returns the stored token: the server refuses it if expired, and the socket's backoff
// reconnect tries the refresh again.
export async function resolveIdToken(signInConfig) {
  const session = readSignInSession();
  if (session === null) return null;
  if (!isIdTokenExpiringSoon(session.idToken)) return session.idToken;
  try {
    const tokenResponse = await requestTokens(signInConfig, {
      grant_type: "refresh_token",
      client_id: signInConfig.appClientId,
      refresh_token: session.refreshToken,
    });
    // Cognito returns a new refresh token only when rotation is on; otherwise the stored one stays valid.
    const refreshToken = typeof tokenResponse.refresh_token === "string" ? tokenResponse.refresh_token : session.refreshToken;
    saveSignInSession({ idToken: tokenResponse.id_token, accessToken: tokenResponse.access_token, refreshToken });
    return tokenResponse.id_token;
  } catch (refreshError) {
    if (!(refreshError instanceof TokenGrantRejected)) {
      console.error("SendPidge: sign-in refresh failed; will retry", describeFailure(refreshError));
      return session.idToken;
    }
    console.error("SendPidge: sign-in refresh refused; signed out", describeFailure(refreshError));
    clearSignInSession();
    return null;
  }
}

// Whether the signed-in user has a passkey (Cognito ListWebAuthnCredentials with the session's access token, refreshed first
// like the ID token, which is issued with it). null when signed out or the call fails.
export async function hasRegisteredPasskey(signInConfig) {
  if (await resolveIdToken(signInConfig) === null) return null;
  try {
    const response = await fetch(signInConfig.identityProviderUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": LIST_PASSKEYS_TARGET },
      body: JSON.stringify({ AccessToken: readSignInSession().accessToken }),
      cache: "no-store",
    });
    if (!response.ok) throw new SignInFailure(`ListWebAuthnCredentials answered ${response.status}`);
    const passkeyListing = await response.json();
    if (!Array.isArray(passkeyListing.Credentials)) throw new SignInFailure("ListWebAuthnCredentials has no Credentials list");
    return passkeyListing.Credentials.length > 0;
  } catch (listError) {
    console.error("SendPidge: could not list the user's passkeys", describeFailure(listError));
    return null;
  }
}

// Managed login's add-a-passkey page, which returns to the app's callback URL.
export function buildAddPasskeyUrl(signInConfig) {
  const addPasskeyUrl = new URL(`${signInConfig.hostedUiBaseUrl}/passkeys/add`);
  addPasskeyUrl.search = new URLSearchParams({ client_id: signInConfig.appClientId, redirect_uri: signInConfig.redirectUrl }).toString();
  return addPasskeyUrl.toString();
}
