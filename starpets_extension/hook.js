/*
 * StarPets Helper — часть, которая работает внутри страницы сайта.
 *
 * 1) Видит запросы, которые сайт сам делает к своему API (fetch и XMLHttpRequest),
 *    и передаёт их расширению: адрес, метод, тело запроса и ответ.
 *    Заголовки (токен входа) расширению не передаются — они остаются здесь.
 * 2) Выполняет запросы расширения так же, как их делает сам сайт: изнутри страницы,
 *    с теми же служебными заголовками (Authorization и т.п.) и куками.
 */
(() => {
  'use strict';
  if (window.__spxHook) return;
  Object.defineProperty(window, '__spxHook', { value: true });

  const MAX_BODY = 3000000;
  const origFetch = window.fetch;
  const XHR = XMLHttpRequest.prototype;
  const origOpen = XHR.open;
  const origSend = XHR.send;
  const origSetHeader = XHR.setRequestHeader;
  // последние служебные заголовки и режим кук, с которыми сайт ходит на каждый хост
  const siteHeaders = new Map();
  const siteCreds = new Map();
  const SKIP_HEADER_RE = /^(content-type|content-length|cookie|host|origin|referer|user-agent|accept-encoding|connection|sec-|proxy-|if-none-match|if-modified-since)/i;
  let seq = 0;

  // с расширением говорим своими событиями (не postMessage) — обработчики сайта их не видят
  const emit = (name, msg) => {
    try {
      document.dispatchEvent(new CustomEvent(name, { detail: JSON.stringify(msg) }));
    } catch (e) {
      // сообщение не ушло — сайту это не мешает
    }
  };
  const post = (msg) => emit('spx:cap', msg);

  function remember(url, headers, creds) {
    try {
      const host = new URL(url).host;
      const keep = {};
      for (const [k, v] of Object.entries(headers)) if (!SKIP_HEADER_RE.test(k)) keep[k.toLowerCase()] = v;
      if (Object.keys(keep).length) siteHeaders.set(host, { ...(siteHeaders.get(host) || {}), ...keep });
      if (creds) siteCreds.set(host, creds);
    } catch (e) {
      // адрес не разобрался — пропускаем
    }
  }

  function bodyOf(body) {
    if (body === undefined || body === null) return ['none', null];
    if (typeof body === 'string') return [/^\s*[[{]/.test(body) ? 'json' : /^[^=&\s]+=[^&]*(&[^=&\s]+=[^&]*)*$/.test(body) ? 'form' : 'text', body];
    if (body instanceof URLSearchParams) return ['form', body.toString()];
    if (body instanceof FormData) {
      const pairs = [];
      for (const [k, v] of body.entries()) {
        if (typeof v !== 'string') return ['binary', null];
        pairs.push([k, v]);
      }
      return ['formdata', JSON.stringify(pairs)];
    }
    return ['binary', null];
  }

  function headersObj(h) {
    const out = {};
    try {
      new Headers(h || {}).forEach((v, k) => {
        out[k] = v;
      });
    } catch (e) {
      // необычные заголовки — пропускаем
    }
    return out;
  }

  const wantsBody = (ct) => !/^(image|video|audio|font)\/|html|javascript|css|event-stream|octet-stream/i.test(ct || '');

  function send(info, status, ct, text) {
    const mutating = !/^(GET|HEAD|OPTIONS)$/.test(info.method);
    if (!mutating && !/^\s*[[{]/.test(text || '')) return;
    post({
      seq: ++seq,
      t: Date.now(),
      method: info.method,
      url: info.url,
      status,
      bodyKind: info.bodyKind,
      body: info.body,
      contentType: info.contentType || '',
      text: text && text.length <= MAX_BODY ? text : '',
      responseType: ct || '',
    });
  }

  // ---------- fetch ----------

  window.fetch = function (input, init) {
    let info = null;
    let pendingBody = null;
    try {
      init = init || {};
      const isReq = typeof Request !== 'undefined' && input instanceof Request;
      const url = new URL(isReq ? input.url : String((input && input.href) || input), location.href).href;
      const method = String(init.method || (isReq ? input.method : 'GET')).toUpperCase();
      const headers = { ...(isReq ? headersObj(input.headers) : {}), ...headersObj(init.headers) };
      const creds = init.credentials || (isReq ? input.credentials : null) || 'same-origin';
      let [bodyKind, body] = bodyOf(init.body);
      if (bodyKind === 'none' && isReq && !/^(GET|HEAD)$/.test(method)) {
        pendingBody = input.clone().text().catch(() => null);
      }
      info = { url, method, bodyKind, body, contentType: headers['content-type'] || '' };
      if (/^https?:/.test(url)) remember(url, headers, creds);
    } catch (e) {
      info = null;
    }
    const p = origFetch.apply(this, arguments);
    if (info) {
      p.then(async (res) => {
        try {
          if (pendingBody) {
            const text = await pendingBody;
            if (text) [info.bodyKind, info.body] = bodyOf(text);
          }
          const ct = res.headers.get('content-type') || '';
          const len = Number(res.headers.get('content-length')) || 0;
          let text = '';
          if (wantsBody(ct) && len <= MAX_BODY) text = await res.clone().text();
          send(info, res.status, ct, text);
        } catch (e) {
          // ответ не прочитался — сайту это не мешает
        }
      }, () => {});
    }
    return p;
  };

  // ---------- XMLHttpRequest ----------

  XHR.open = function (method, url) {
    try {
      this.__spx = { method: String(method || 'GET').toUpperCase(), url: String(url), headers: {} };
    } catch (e) {
      this.__spx = null;
    }
    return origOpen.apply(this, arguments);
  };

  XHR.setRequestHeader = function (name, value) {
    try {
      if (this.__spx) this.__spx.headers[String(name).toLowerCase()] = String(value);
    } catch (e) {
      // не страшно
    }
    return origSetHeader.apply(this, arguments);
  };

  XHR.send = function (body) {
    const info = this.__spx;
    if (info) {
      try {
        info.url = new URL(info.url, location.href).href;
        [info.bodyKind, info.body] = bodyOf(body);
        info.contentType = info.headers['content-type'] || '';
        if (/^https?:/.test(info.url)) remember(info.url, info.headers, this.withCredentials ? 'include' : 'same-origin');
        this.addEventListener('loadend', () => {
          try {
            const ct = this.getResponseHeader('content-type') || '';
            let text = '';
            if (this.responseType === '' || this.responseType === 'text') text = wantsBody(ct) ? this.responseText : '';
            else if (this.responseType === 'json' && this.response !== null) text = JSON.stringify(this.response);
            if (this.status) send(info, this.status, ct, text);
          } catch (e) {
            // не страшно
          }
        });
      } catch (e) {
        // не страшно
      }
    }
    return origSend.apply(this, arguments);
  };

  // ---------- запросы расширения ----------

  function headersFor(host) {
    if (siteHeaders.has(host)) return { ...siteHeaders.get(host) };
    // на этот хост сайт ещё не ходил — берём заголовки входа с другого хоста того же сайта
    const base = host.split('.').slice(-2).join('.');
    for (const [h, v] of siteHeaders) if (h.split('.').slice(-2).join('.') === base && v.authorization) return { ...v };
    return {};
  }

  document.addEventListener('spx:req', async (e) => {
    let d;
    try {
      d = JSON.parse(e.detail);
    } catch (err) {
      return;
    }
    let status = 0;
    let text = '';
    let error = '';
    try {
      const u = new URL(d.url, location.href);
      const headers = headersFor(u.host);
      const init = { method: d.method, headers, credentials: siteCreds.get(u.host) || 'same-origin', cache: 'no-store' };
      if (d.bodyKind === 'json') {
        headers['content-type'] = d.contentType || 'application/json';
        init.body = d.body;
      } else if (d.bodyKind === 'form') {
        headers['content-type'] = d.contentType || 'application/x-www-form-urlencoded;charset=UTF-8';
        init.body = d.body;
      } else if (d.bodyKind === 'formdata') {
        const fd = new FormData();
        for (const [k, v] of JSON.parse(d.body)) fd.append(k, v);
        init.body = fd;
      } else if (d.bodyKind === 'text') {
        if (d.contentType) headers['content-type'] = d.contentType;
        init.body = d.body;
      }
      const res = await origFetch.call(window, u.href, init);
      status = res.status;
      text = await res.text();
    } catch (err) {
      error = String((err && err.message) || err);
    }
    emit('spx:res', { id: d.id, status, text, error });
  });
})();
