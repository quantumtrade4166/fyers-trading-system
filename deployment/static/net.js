/* net.js — one safety layer under every data call the dashboard page makes (16-Nov-2026).

   The page has ~80 fetch() calls and none had a timeout or an in-flight guard, so:
     * one hung call left a tab sitting there forever with no error (you had to refresh), and
     * a 1-second poll kept firing new requests while the old ones were still waiting, until the
       browser's connection queue was full and every tab stalled.

   This wraps window.fetch for same-site GETs only:
     1. TIMEOUT   - a GET that has not answered in 20 s is cancelled, so the caller's own
                    try/catch runs and the next poll gets a clean start.
     2. NO PILE-UP - while a GET for a URL is still in flight, a second identical GET joins it
                    instead of opening another request.
     3. BADGE     - after two failures in a row a small "Connection slow" pill shows; it goes
                    away on the next good answer.

   Never touched: POST/PUT/DELETE (arm, kill, deploy, orders, controls), calls that pass their own
   AbortSignal, other sites (CDN), and the long-running DualMom service calls. After any non-GET
   finishes, the join table is cleared so the page's follow-up read is always a fresh request. */
(function () {
  "use strict";
  if (window.__netPatched || !window.fetch || !window.AbortController) return;
  window.__netPatched = true;

  var raw = window.fetch.bind(window);
  var GET_TIMEOUT_MS = 20000;
  var NO_TIMEOUT = ["/api/dualmom/live/"];        // previews / rebalances can legitimately take minutes
  var inflight = new Map();                       // url -> Promise<Response> (never read directly, only cloned)
  var fails = 0, pill = null;

  function urlOf(input) {
    try { return typeof input === "string" ? input : (input && input.url) || String(input); }
    catch (e) { return ""; }
  }
  function methodOf(input, opts) {
    return String((opts && opts.method) || (input && typeof input === "object" && input.method) || "GET").toUpperCase();
  }
  function sameSite(url) {
    if (!url) return false;
    if (url.charAt(0) === "/" && url.charAt(1) !== "/") return true;
    if (url.indexOf(location.origin + "/") === 0) return true;
    return !/^[a-z][a-z0-9+.-]*:/i.test(url) && url.indexOf("//") !== 0;   // relative path
  }

  function showPill(on) {
    try {
      if (!on) { if (pill) pill.style.display = "none"; return; }
      if (!pill) {
        pill = document.createElement("div");
        pill.id = "net-pill";
        pill.textContent = "Connection slow — retrying…";
        pill.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:99999;padding:7px 12px;" +
          "border-radius:999px;font:600 12px Inter,system-ui,sans-serif;color:#1a1300;background:#f5a623;" +
          "box-shadow:0 4px 14px rgba(0,0,0,.45);pointer-events:none";
        (document.body || document.documentElement).appendChild(pill);
      }
      pill.style.display = "";
    } catch (e) { /* the badge must never break a data call */ }
  }
  function good() { fails = 0; showPill(false); }
  function bad() { fails += 1; if (fails >= 2) showPill(true); }

  window.fetch = function (input, opts) {
    var url = urlOf(input), method = methodOf(input, opts);

    if (method !== "GET") {
      // controls and orders go through untouched; afterwards every read must be a fresh request
      var done = function () { inflight.clear(); };
      var p0 = raw(input, opts);
      p0.then(done, done);
      return p0;
    }
    if (!sameSite(url) || (opts && opts.signal)) return raw(input, opts);

    var key = url + "|" + ((opts && opts.cache) || "");
    var joined = inflight.get(key);
    if (joined) return joined.then(function (r) { return r.clone(); });

    var limit = GET_TIMEOUT_MS;
    for (var i = 0; i < NO_TIMEOUT.length; i++) if (url.indexOf(NO_TIMEOUT[i]) !== -1) limit = 0;

    var ctl = new AbortController(), timer = null;
    if (limit) timer = setTimeout(function () { ctl.abort(); }, limit);
    var o = {};
    if (opts) for (var k in opts) o[k] = opts[k];
    o.signal = ctl.signal;

    var p = raw(input, o).then(function (r) {
      if (r.status >= 502 && r.status <= 504) bad(); else good();
      return r;
    }, function (e) { bad(); throw e; });
    var settle = function () { if (timer) clearTimeout(timer); if (inflight.get(key) === p) inflight.delete(key); };
    p.then(settle, settle);
    inflight.set(key, p);
    return p.then(function (r) { return r.clone(); });
  };

  window.__net = { inflight: inflight, raw: raw, timeoutMs: GET_TIMEOUT_MS };
})();
