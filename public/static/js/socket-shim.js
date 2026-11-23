// Drop-in for the bits of the Socket.IO client these pages use: io(), socket.on(), socket.emit().
// Talks plain WebSocket to the Cloudflare Durable Object at /ws and reconnects on its own
// (phones sleep and switch networks a lot).
//
// Staff pages load it with `data-staff`: they ask for the event PIN once, keep it on this
// device, and send it with every scoring/admin action. The display and rankings pages don't.
(function () {
  const STAFF = document.currentScript && document.currentScript.hasAttribute('data-staff');
  const KEY = 'irish-event-pin';
  // Under /demo/ the same pages talk to a throwaway copy of the field: no PIN, no real data.
  const DEMO = location.pathname === '/demo' || location.pathname.startsWith('/demo/');
  const BASE = DEMO ? '/demo' : '';

  function getPin() {
    if (DEMO) return null;
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function askPin(message) {
    const pin = (window.prompt(message) || '').trim();
    try { pin ? localStorage.setItem(KEY, pin) : localStorage.removeItem(KEY); } catch (e) { /* private mode */ }
    return pin;
  }
  if (STAFF && !DEMO && !getPin()) askPin('Enter the event PIN');

  // API calls carry the PIN; a 401 asks for it again once and retries.
  const rawFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    init = init || {};
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith('/api/')) return rawFetch(input, init);
    const send = () => {
      const headers = new Headers(init.headers || {});
      const pin = getPin();
      if (pin) headers.set('X-Event-PIN', pin);
      return rawFetch(BASE + url, Object.assign({}, init, { headers }));
    };
    let res = await send();
    if (res.status === 401 && !DEMO && askPin('Wrong or missing event PIN. Enter it again:')) res = await send();
    return res;
  };

  window.io = function () {
    const handlers = {};
    const queue = [];
    let ws;
    let askedThisSocket = false;

    const fire = (event, data) => (handlers[event] || []).forEach((h) => h(data));
    const raw = (event, data) => JSON.stringify({ event, data: data || {} });

    function connect() {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}${BASE}/ws`);
      askedThisSocket = false;
      ws.onopen = () => {
        const pin = getPin();
        if (pin) ws.send(raw('auth', { pin }));
        ws.send(raw('sync')); // catch up on a match already running
        while (queue.length) ws.send(queue.shift());
        fire('connect');
      };
      ws.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch (err) { return; }
        const badPin = msg.event === 'auth_error' || (msg.event === 'auth_result' && !msg.data.ok);
        if (badPin && STAFF && !DEMO && !askedThisSocket) {
          askedThisSocket = true;
          const pin = askPin('Wrong or missing event PIN. Enter it again, then repeat your last tap:');
          if (pin) { ws.send(raw('auth', { pin })); askedThisSocket = false; }
        }
        fire(msg.event, msg.data);
      };
      ws.onclose = () => {
        fire('disconnect');
        setTimeout(connect, 1000);
      };
    }
    connect();

    return {
      on(event, handler) { (handlers[event] = handlers[event] || []).push(handler); },
      emit(event, data) {
        const m = raw(event, data);
        if (ws.readyState === WebSocket.OPEN) ws.send(m); else queue.push(m);
      },
    };
  };

  if (DEMO && location.pathname.replace(/\/$/, '') !== '/demo') {
    const add = () => {
      const a = document.createElement('a');
      a.href = '/demo/';
      a.textContent = 'DEMO · not real data';
      a.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:8px;z-index:99999;padding:4px 10px;border-radius:999px;'
        + 'background:#f5b800;color:#111;font:600 12px system-ui,sans-serif;text-decoration:none;opacity:.9';
      document.body.appendChild(a);
    };
    document.body ? add() : document.addEventListener('DOMContentLoaded', add);
  }
})();
