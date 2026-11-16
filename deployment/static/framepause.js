/* framepause.js — a strategy page inside the dashboard stops polling while its tab is not shown
   (16-Nov-2026).

   CE/PE Imbalance, Strategy Builder, ST Directional and Taker Flow are loaded as frames inside the
   dashboard. Once opened they kept every timer running forever (up to 2-3 requests a second each),
   even with another dashboard tab in front: `document.hidden` is false inside a frame whose tab is
   merely switched away. After visiting a few tabs the browser was sending a dozen requests a second
   for pages nobody was looking at, and the whole dashboard stalled until a refresh.

   What this does, only when the page is running inside a frame:
     * every setInterval callback is skipped while the frame is hidden (display:none in the parent);
     * the moment the frame is shown again, every callback that was skipped runs once straight away,
       so the page is fresh at once instead of waiting for its next tick.

   Opened directly in its own browser tab the page behaves exactly as before. Display only: nothing
   here sends, changes or delays an order, and setTimeout / streams / websockets are not touched. */
(function () {
  "use strict";
  var framed = false;
  try { framed = window.self !== window.top; } catch (e) { framed = true; }
  if (!framed || window.__framePause) return;

  function hidden() {
    try { var f = window.frameElement; return !!f && f.offsetParent === null; }
    catch (e) { return false; }                 // cannot tell -> never pause
  }

  var _set = window.setInterval, _clear = window.clearInterval;
  var jobs = new Map();                         // interval id -> {fn, args, missed}

  window.setInterval = function (fn, ms) {
    if (typeof fn !== "function") return _set.apply(window, arguments);
    var args = Array.prototype.slice.call(arguments, 2);
    var job = { fn: fn, args: args, missed: false };
    var id = _set(function () {
      if (hidden()) { job.missed = true; return; }
      fn.apply(window, args);
    }, ms);
    jobs.set(id, job);
    return id;
  };
  window.clearInterval = function (id) { jobs.delete(id); return _clear(id); };

  var was = hidden();
  _set(function () {
    var now = hidden();
    if (was && !now) {
      jobs.forEach(function (job) {
        if (!job.missed) return;
        job.missed = false;
        try { job.fn.apply(window, job.args); } catch (e) { /* one page timer must not stop the rest */ }
      });
    }
    was = now;
  }, 250);

  window.__framePause = { hidden: hidden, jobs: jobs };
})();
