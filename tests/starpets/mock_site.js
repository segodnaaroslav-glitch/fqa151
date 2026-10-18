/*
 * Имитация starpets.gg для проверки расширения без настоящего сайта.
 * Настоящий API StarPets расширению заранее не известен, поэтому имитация
 * бывает двух очень разных видов — расширение должно научиться обоим.
 *
 * variant "a": API на отдельном хосте api.starpets.gg, вход по заголовку
 *   Authorization: Bearer (токен в localStorage), сайт ходит через fetch.
 *   Ваши лоты: GET /api/v2/users/me/sales?page=1&limit=20 (сайт грузит только
 *   первую страницу), цена в долларах числом, у каждого варианта питомца свой товар.
 *   Лоты товара: GET /api/v2/products/{id}/sales?sort=price_asc.
 *   Смена цены: PATCH /api/v2/sales/{id} {"price": "12.34"}.
 *
 * variant "b": API на том же хосте, вход по httpOnly-куке + заголовок
 *   X-CSRF-Token, сайт ходит через XMLHttpRequest.
 *   Ваши лоты: GET /api/market/my/offers?offset=0&limit=10, цена строкой в долларах.
 *   Лоты товара: GET /api/market/catalog/{slug имени}/offers?fly=1&ride=0&neon=1&mega=0
 *   — сервер параметры вариантов НЕ учитывает и отдаёт все варианты питомца.
 *   Смена цены: DELETE /api/market/offers/{offer_id}, затем
 *   POST /api/market/offers {"item_id": "...", "price": 1250} — цена в центах.
 */
'use strict';

const http = require('http');

const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const V = (s) => ({ mega: s.includes('M'), neon: s.includes('N'), fly: s.includes('F'), ride: s.includes('R') });

// товары: [имя, вариант]; цены: мои и других продавцов
const CATALOG = [
  ['Frost Dragon', 'NFR'],
  ['Frost Dragon', 'FR'],
  ['Shadow Dragon', 'MFR'],
  ['Bat Dragon', ''],
  ['Giraffe', 'R'],
  ['Owl', 'F'],
  ['Unicorn', ''],
  ['Turtle', 'NFR'],
];
// мои лоты: [№ товара, цена]
const MINE = [
  [0, 120.0], [0, 118.5], [0, 125.0], [1, 64.0], [1, 70.0], [2, 410.0], [3, 99.0], [3, 95.5],
  [4, 33.0], [4, 30.25], [4, 41.0], [5, 18.0], [5, 17.5], [6, 12.0], [6, 12.0], [7, 40.0],
  [0, 130.0], [1, 66.6], [2, 399.99], [3, 101.0], [4, 29.99], [5, 19.0], [5, 22.0], [6, 13.0],
];
// лоты других продавцов: [№ товара, цена]; у Unicorn (6) других продавцов нет
const OTHERS = [
  [0, 115.0], [0, 119.0], [0, 140.0], [1, 63.5], [1, 65.0], [2, 405.0], [2, 450.0], [3, 97.0],
  [4, 31.0], [4, 35.0], [5, 17.0], [5, 25.0], [7, 15.0], [7, 16.0],
];
// для варианта b: дешёвые лоты того же имени, но другого варианта (их нельзя учитывать)
const DECOYS = [['Frost Dragon', '', 20.0], ['Frost Dragon', 'MFR', 30.0], ['Giraffe', '', 5.0], ['Turtle', '', 2.0], ['Owl', 'FR', 9.0]];

function initialState(variant) {
  const lots = [];
  let n = 0;
  const add = (pi, price, mine, extra = {}) => {
    n++;
    const [name, v] = typeof pi === 'number' ? CATALOG[pi] : pi;
    lots.push({ n, name, v, pid: `p-${slug(name)}-${v.toLowerCase() || 'plain'}`, price, mine, status: 'active', item: `itm_${String(1000 + n)}abc`, ...extra });
  };
  for (const [pi, price] of MINE) add(pi, price, true);
  for (const [pi, price] of OTHERS) add(pi, price, false);
  if (variant === 'b') for (const [name, v, price] of DECOYS) add([name, v], price, false);
  // давно проданный ваш лот — не должен попасть в сумму
  add(0, 500.0, true, { status: 'sold' });
  return { variant, lots, nextId: 50000, csrf: 'csrf-' + Math.random().toString(36).slice(2), sid: 'sid-' + Math.random().toString(36).slice(2), token: 'tok-' + Math.random().toString(36).slice(2), requests: [] };
}

// ---------- представление лотов ----------

function saleA(l) {
  return {
    id: `s-${l.n}`,
    price: l.price,
    currency: 'usd',
    status: l.status,
    created_at: '2026-10-01T10:00:00Z',
    seller: l.mine ? { id: 'u-me-42', username: 'tester' } : { id: `u-${(l.n % 5) + 1}00`, username: `seller${l.n % 5}` },
    product: { id: l.pid, name: l.name, rarity: 'legendary', image: `https://cdn.starpets.gg/${l.pid}.png` },
    item: { id: `it-${l.n}`, neon: V(l.v).neon, mega_neon: V(l.v).mega, flyable: V(l.v).fly, rideable: V(l.v).ride },
  };
}

function offerB(l) {
  return {
    offer_id: l.offerId || 9000 + l.n,
    item_id: l.item,
    name: l.name,
    price: l.price.toFixed(2),
    props: V(l.v),
    owner_id: l.mine ? 777777 : 100000 + l.n,
    status: l.status === 'active' ? 'listed' : l.status,
  };
}

// ---------- страницы ----------

const PAGE_A = (title, script) => `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head>
<body style="font-family:sans-serif;background:#111;color:#eee"><h1>${title}</h1><div id="app">Загрузка…</div>
<script>
const API = 'http://api.starpets.gg/api/v2';
if (!localStorage.getItem('token')) localStorage.setItem('token', '__TOKEN__');
const auth = () => ({ Authorization: 'Bearer ' + localStorage.getItem('token') });
fetch(API + '/users/me', { headers: auth() });
${script}
</script></body></html>`;

const SALES_A = `
async function load() {
  const r = await fetch(API + '/users/me/sales?page=1&limit=20', { headers: auth() });
  const j = await r.json();
  const app = document.getElementById('app');
  app.innerHTML = '';
  for (const s of j.data.items) {
    const row = document.createElement('div');
    row.className = 'sale';
    row.dataset.id = s.id;
    row.innerHTML = '<span class="name"></span> <input class="price-input"> <button class="save">Сохранить</button>';
    row.querySelector('.name').textContent = s.product.name + ' ' + s.price;
    row.querySelector('.price-input').value = s.price;
    row.querySelector('.save').onclick = async () => {
      const price = row.querySelector('.price-input').value;
      await fetch(API + '/sales/' + s.id, { method: 'PATCH', headers: { ...auth(), 'Content-Type': 'application/json' }, body: JSON.stringify({ price: String(price) }) });
      load();
    };
    app.appendChild(row);
  }
}
load();`;

const PRODUCT_A = `
const pid = location.pathname.split('/').pop();
fetch(API + '/products/' + pid + '/sales?sort=price_asc&limit=30').then(r => r.json()).then(j => {
  document.getElementById('app').textContent = j.data.map(s => s.price).join(', ');
});`;

const PAGE_B = (title, script, csrf) => `<!doctype html><html><head><meta charset="utf-8"><meta name="csrf-token" content="${csrf}"><title>${title}</title></head>
<body style="font-family:sans-serif;background:#111;color:#eee"><h1>${title}</h1><div id="app">Загрузка…</div>
<script>
function api(method, url, body) {
  return new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open(method, url);
    x.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name=csrf-token]').content);
    if (body) x.setRequestHeader('Content-Type', 'application/json');
    x.onload = () => resolve({ status: x.status, json: x.responseText ? JSON.parse(x.responseText) : null });
    x.send(body ? JSON.stringify(body) : null);
  });
}
api('GET', '/api/market/me');
${script}
</script></body></html>`;

const SALES_B = `
async function load() {
  const r = await api('GET', '/api/market/my/offers?offset=0&limit=10');
  const app = document.getElementById('app');
  app.innerHTML = '';
  for (const o of r.json.offers) {
    const row = document.createElement('div');
    row.className = 'sale';
    row.innerHTML = '<span class="name"></span> <input class="price-input"> <button class="save">Сохранить</button>';
    row.querySelector('.name').textContent = o.name + ' ' + o.price;
    row.querySelector('.price-input').value = o.price;
    row.querySelector('.save').onclick = async () => {
      const cents = Math.round(parseFloat(row.querySelector('.price-input').value) * 100);
      await api('DELETE', '/api/market/offers/' + o.offer_id);
      await api('POST', '/api/market/offers', { item_id: o.item_id, price: cents });
      load();
    };
    app.appendChild(row);
  }
}
load();`;

const PRODUCT_B = `
const p = new URLSearchParams(location.search);
const name = location.pathname.split('/').pop();
const q = ['fly', 'ride', 'neon', 'mega'].map(k => k + '=' + (p.get(k) || '0')).join('&');
api('GET', '/api/market/catalog/' + name + '/offers?' + q + '&sort=price_asc').then(r => {
  document.getElementById('app').textContent = r.json.offers.map(o => o.price).join(', ');
});`;

// ---------- сервер ----------

function start(variant, port = 0) {
  const state = initialState(variant);
  const send = (res, status, body, headers = {}) => {
    const text = body === null || body === undefined ? '' : typeof body === 'string' ? body : JSON.stringify(body);
    const type = typeof body === 'string' && body.startsWith('<!doctype') ? 'text/html; charset=utf-8' : 'application/json';
    res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', ...headers });
    res.end(text);
  };

  const server = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      const host = String(req.headers.host || '').split(':')[0];
      const u = new URL(req.url, `http://${host}`);
      const body = raw ? (() => { try { return JSON.parse(raw); } catch (e) { return raw; } })() : null;
      if (req.method !== 'OPTIONS' && u.pathname.startsWith('/api/')) state.requests.push({ method: req.method, host, path: u.pathname, query: u.search, body });
      try {
        (variant === 'a' ? routeA : routeB)(req, res, host, u, body);
      } catch (e) {
        send(res, 500, { error: String(e) });
      }
    });
  });

  function routeA(req, res, host, u, body) {
    if (host === 'starpets.gg') {
      if (u.pathname === '/profile/sales') return send(res, 200, PAGE_A('Мои продажи', SALES_A).replace('__TOKEN__', state.token));
      if (u.pathname.startsWith('/product/')) return send(res, 200, PAGE_A('Товар', PRODUCT_A).replace('__TOKEN__', state.token));
      return send(res, 200, PAGE_A('StarPets', '').replace('__TOKEN__', state.token));
    }
    const cors = {
      'Access-Control-Allow-Origin': 'http://starpets.gg',
      'Access-Control-Allow-Headers': 'authorization, content-type',
      'Access-Control-Allow-Methods': 'GET, POST, PATCH, PUT, DELETE, OPTIONS',
    };
    if (req.method === 'OPTIONS') return send(res, 204, null, cors);
    const authed = req.headers.authorization === `Bearer ${state.token}`;
    const p = u.pathname;
    let m;
    if (p === '/api/v2/users/me') return authed ? send(res, 200, { data: { id: 'u-me-42', username: 'tester', balance: 3.5 } }, cors) : send(res, 401, { error: 'unauthorized' }, cors);
    if (p === '/api/v2/users/me/sales' && req.method === 'GET') {
      if (!authed) return send(res, 401, { error: 'unauthorized' }, cors);
      const page = Number(u.searchParams.get('page') || 1);
      const limit = Number(u.searchParams.get('limit') || 20);
      const mine = state.lots.filter((l) => l.mine);
      const items = mine.slice((page - 1) * limit, page * limit).map(saleA);
      return send(res, 200, { data: { items, total: mine.length, page, limit } }, cors);
    }
    if ((m = p.match(/^\/api\/v2\/products\/([^/]+)\/sales$/)) && req.method === 'GET') {
      const lots = state.lots.filter((l) => l.pid === m[1] && l.status === 'active').sort((a, b) => a.price - b.price);
      return send(res, 200, { data: lots.map(saleA) }, cors);
    }
    if ((m = p.match(/^\/api\/v2\/sales\/s-(\d+)$/)) && req.method === 'PATCH') {
      if (!authed) return send(res, 401, { error: 'unauthorized' }, cors);
      const l = state.lots.find((x) => x.n === Number(m[1]) && x.mine && x.status === 'active');
      if (!l) return send(res, 404, { error: 'not found' }, cors);
      if (!body || typeof body.price !== 'string' || !/^\d+(\.\d{1,2})?$/.test(body.price)) return send(res, 422, { error: 'price must be a string with 2 decimals' }, cors);
      l.price = Number(body.price);
      return send(res, 200, { data: saleA(l) }, cors);
    }
    return send(res, 404, { error: 'no route' }, cors);
  }

  function routeB(req, res, host, u, body) {
    const p = u.pathname;
    if (!p.startsWith('/api/')) {
      const cookie = `sid=${state.sid}; HttpOnly; Path=/`;
      if (p === '/profile/sales') return send(res, 200, PAGE_B('Мои продажи', SALES_B, state.csrf), { 'Set-Cookie': cookie });
      if (p.startsWith('/catalog/')) return send(res, 200, PAGE_B('Товар', PRODUCT_B, state.csrf), { 'Set-Cookie': cookie });
      return send(res, 200, PAGE_B('StarPets', '', state.csrf), { 'Set-Cookie': cookie });
    }
    const authed = String(req.headers.cookie || '').includes(`sid=${state.sid}`);
    const csrfOk = req.headers['x-csrf-token'] === state.csrf;
    let m;
    if (p === '/api/market/me') return authed ? send(res, 200, { user: { id: 777777, nickname: 'tester' } }) : send(res, 401, { error: 'login' });
    if (p === '/api/market/my/offers' && req.method === 'GET') {
      if (!authed) return send(res, 401, { error: 'login' });
      const offset = Number(u.searchParams.get('offset') || 0);
      const limit = Number(u.searchParams.get('limit') || 10);
      const mine = state.lots.filter((l) => l.mine && (l.status === 'active' || l.status === 'sold'));
      return send(res, 200, { offers: mine.slice(offset, offset + limit).map(offerB), total: mine.length });
    }
    if ((m = p.match(/^\/api\/market\/catalog\/([^/]+)\/offers$/)) && req.method === 'GET') {
      // параметры вариантов (fly, ride, neon, mega) сервер не учитывает
      const lots = state.lots.filter((l) => slug(l.name) === m[1] && l.status === 'active').sort((a, b) => a.price - b.price);
      return send(res, 200, { offers: lots.map(offerB) });
    }
    if (!authed || !csrfOk) return send(res, 403, { error: 'csrf' });
    if ((m = p.match(/^\/api\/market\/offers\/(\d+)$/)) && req.method === 'DELETE') {
      const l = state.lots.find((x) => x.mine && x.status === 'active' && offerB(x).offer_id === Number(m[1]));
      if (!l) return send(res, 404, { error: 'not found' });
      l.status = 'off';
      return send(res, 204, null);
    }
    if (p === '/api/market/offers' && req.method === 'POST') {
      const l = state.lots.find((x) => x.mine && x.item === (body && body.item_id) && x.status !== 'sold');
      if (!l) return send(res, 404, { error: 'no item' });
      if (l.status === 'active') return send(res, 409, { error: 'already listed' });
      if (!Number.isInteger(body.price) || body.price < 1) return send(res, 422, { error: 'price in cents' });
      l.price = body.price / 100;
      l.status = 'active';
      l.offerId = ++state.nextId;
      return send(res, 201, { offer: offerB(l) });
    }
    return send(res, 404, { error: 'no route' });
  }

  return new Promise((resolve) => {
    server.listen(port, '127.0.0.1', () => {
      resolve({ port: server.address().port, state, close: () => new Promise((r) => server.close(r)) });
    });
  });
}

module.exports = { start, CATALOG, V, slug };

if (require.main === module) {
  const variant = process.argv[2] || 'a';
  start(variant, Number(process.argv[3] || 0)).then(({ port }) => console.log(`mock ${variant} on 127.0.0.1:${port}`));
}
