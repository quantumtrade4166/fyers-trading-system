"""
mcx_terminal/access.py — the terminal's OWN login lock (second lock behind Cloudflare Access).
=============================================================================================

mcx.trading.contact is protected by Cloudflare Access (email code). This middleware makes the
terminal itself verify that, so a mis-set Cloudflare rule can never open it to the world:

  * A request is EXTERNAL when it came through Cloudflare (Cf-Ray / Cf-Connecting-Ip header) or its
    Host is not localhost. External requests must carry Cloudflare Access's signed token
    (Cf-Access-Jwt-Assertion header or CF_Authorization cookie), and it must verify:
      RS256 signature against https://<team>.cloudflareaccess.com/cdn-cgi/access/certs,
      audience = the Access application's AUD tag, not expired,
      email in MCX_ACCESS_EMAILS (when that is set).
  * FAIL CLOSED: until MCX_ACCESS_TEAM and MCX_ACCESS_AUD are in deployment/.env, every external
    request is refused (403 / websocket close 4403).
  * Local requests (an SSH tunnel to 127.0.0.1:47680) are allowed: only someone already on the VPS
    can make them. The server binds 127.0.0.1, so nothing else can reach it at all.

Works for both HTTP and WebSocket (pure ASGI middleware).
"""
import logging
import os
import threading
import time

log = logging.getLogger("mcx.access")

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class AccessGuard:
    def __init__(self, app, team: str | None = None, aud: str | None = None, emails: str | None = None):
        self.app = app
        self.team = (team if team is not None else os.getenv("MCX_ACCESS_TEAM", "")).strip()
        a = aud if aud is not None else os.getenv("MCX_ACCESS_AUD", "")
        self.aud = [x.strip() for x in a.split(",") if x.strip()]        # one or more Access application AUD tags
        em = emails if emails is not None else os.getenv("MCX_ACCESS_EMAILS", "")
        self.emails = {e.strip().lower() for e in em.split(",") if e.strip()}
        self._jwks = None
        self._jwks_at = 0.0
        self._lock = threading.Lock()
        self._denied_log_at = 0.0
        if not (self.team and self.aud):
            log.warning("Access lock NOT configured (MCX_ACCESS_TEAM / MCX_ACCESS_AUD): all external requests refused")

    # ── decision ────────────────────────────────────────────────────────────
    @staticmethod
    def _headers(scope) -> dict:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

    @staticmethod
    def is_external(h: dict) -> bool:
        if "cf-ray" in h or "cf-connecting-ip" in h or "cf-access-jwt-assertion" in h:
            return True
        host = h.get("host", "").lower()
        host = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return host not in LOCAL_HOSTS

    @staticmethod
    def token_of(h: dict) -> str | None:
        t = h.get("cf-access-jwt-assertion")
        if t:
            return t.strip()
        for part in h.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "CF_Authorization" and v:
                return v
        return None

    def _keys(self):
        import jwt
        with self._lock:
            if self._jwks is None or time.time() - self._jwks_at > 3600:
                self._jwks = jwt.PyJWKClient(f"https://{self.team}.cloudflareaccess.com/cdn-cgi/access/certs")
                self._jwks_at = time.time()
            return self._jwks

    def verify(self, token: str) -> tuple[bool, str]:
        if not (self.team and self.aud):
            return False, "login lock not configured yet"
        if not token:
            return False, "no Cloudflare Access token"
        import jwt
        try:
            key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.aud,
                                issuer=f"https://{self.team}.cloudflareaccess.com")
        except Exception as e:
            return False, f"token rejected: {type(e).__name__}"
        email = str(claims.get("email", "")).lower()
        if self.emails and email not in self.emails:
            return False, f"{email or 'unknown user'} is not allowed"
        return True, email

    # ── ASGI ────────────────────────────────────────────────────────────────
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        h = self._headers(scope)
        if not self.is_external(h):
            scope["mcx_user"] = "local"                 # on the VPS itself / through an SSH tunnel
            return await self.app(scope, receive, send)
        ok, why = self.verify(self.token_of(h))
        if ok:
            scope["mcx_user"] = why                     # the verified Cloudflare email (decides who may arm LIVE)
            return await self.app(scope, receive, send)
        now = time.time()
        if now - self._denied_log_at > 30:
            self._denied_log_at = now
            log.warning("refused external %s %s from %s: %s", scope["type"], scope.get("path"),
                        h.get("cf-connecting-ip", "?"), why)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = f"403 - MCX Desk: {why}".encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
