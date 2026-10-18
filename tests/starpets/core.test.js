// Проверки логики расширения без браузера. Запуск: node --test tests/starpets/
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../../starpets_extension/core.js');

const sale = (n, price, extra = {}) => ({
  id: `s-${n}`,
  price,
  currency: 'usd',
  status: 'active',
  seller: { id: 'u-me-42', username: 'tester' },
  product: { id: `p-${n % 3}`, name: ['Frost Dragon', 'Owl', 'Giraffe'][n % 3] },
  item: { id: `it-${n}`, neon: n % 3 === 0, flyable: true, rideable: n % 2 === 0 },
  ...extra,
});

const cap = (seq, method, url, json, extra = {}) => ({
  seq, t: 1000 + seq * 100, method, url, status: 200, bodyKind: 'none', body: null, contentType: '', json, ...extra,
});

test('цена, id и имя из лота', () => {
  const s = sale(1, 12.5);
  assert.deepEqual(C.listPrice(s), { path: 'price', value: 12.5, score: 100 });
  assert.equal(C.listPrice({ id: 1, price: { amount: '7,50', currency: 'usd' } }).value, 7.5);
  assert.equal(C.listPrice({ id: 1, min_price: 3, price_cents: 1250 }).value, 12.5);
  assert.equal(C.listPrice({ id: 1, market_price: 3 }), null);
  assert.equal(C.itemName(s), 'Owl');
  assert.equal(C.itemName({ seller: { name: 'bob' }, pet: { name: 'Cat' } }), 'Cat');
  assert.equal(C.variantLabel(sale(3, 1)), 'NF');
  assert.equal(C.variantLabel({ props: ['mega', 'fly', 'ride'] }), 'MFR');
  assert.equal(C.toPrice('1 234,50'), 1234.5);
  assert.equal(C.toPrice('abc'), null);
  assert.ok(!C.isActive({ status: 'sold' }) && C.isActive({ status: 'listed' }));
});

test('список ваших товаров: id, ваш id продавца, активные', () => {
  const list = [sale(1, 10), sale(2, 20), sale(3, 30), sale(4, 40, { status: 'sold' })];
  const d = C.describeMine(list);
  assert.equal(d.list.length, 3);
  assert.equal(d.skipped, 1);
  assert.deepEqual(d.ownPaths, ['id', 'item.id']);
  assert.equal(d.uid, 'u-me-42');
  assert.equal(d.currency, '$');
  assert.ok(C.isMine({ id: 'x', seller: { id: 'u-me-42' }, price: 1 }, d));
  assert.ok(C.isMine({ id: 's-2', price: 1 }, d));
  assert.ok(!C.isMine({ id: 's-99', seller: { id: 'u-7' }, price: 1, product: { id: 'p-1' } }, d));
});

test('угадывание списка ваших товаров по адресу', () => {
  const body = { data: { items: [sale(1, 10), sale(2, 20)], total: 2 } };
  const g = C.guessMine(cap(1, 'GET', 'https://api.starpets.gg/api/v2/users/me/sales?page=1', body));
  assert.equal(g.op.listPath, 'data.items');
  assert.equal(g.list.length, 2);
  assert.equal(C.guessMine(cap(2, 'GET', 'https://api.starpets.gg/api/v2/products/p1/sales', body)), null);
  assert.equal(C.guessMine(cap(3, 'GET', 'https://api.starpets.gg/api/v2/users/me/history', body)), null);
});

test('обучение смене цены: PATCH с id в адресе', () => {
  const list = [sale(1, 10), sale(2, 20), sale(3, 30)];
  const listCap = cap(1, 'GET', 'https://api.starpets.gg/api/v2/users/me/sales?page=1&limit=20', { data: { items: list } });
  const lists = [{ cap: listCap, path: 'data.items', list }];
  const edit = cap(2, 'PATCH', 'https://api.starpets.gg/api/v2/sales/s-2', { data: {} }, { bodyKind: 'json', body: '{"price":"21.00"}', contentType: 'application/json' });
  const r = C.learnEdit(edit, lists, [edit]);
  assert.equal(r.edit.steps.length, 1);
  assert.equal(C.describeTemplate(r.edit.steps[0]), 'PATCH /api/v2/sales/{id}');
  assert.equal(r.mine.listPath, 'data.items');
  assert.equal(r.reqPrice, 21);
  const req = C.fillTemplate(r.edit.steps[0], list[2], 27.9, 1);
  assert.equal(req.url, 'https://api.starpets.gg/api/v2/sales/s-3');
  assert.equal(req.body, '{"price":"27.90"}');
  assert.equal(C.detectScale(21, 21), 1);
});

test('обучение смене цены: снять и выставить заново, цена в центах', () => {
  const offer = (n, price) => ({ offer_id: 9000 + n, item_id: `itm_${n}abc`, name: 'Frost Dragon', price: price.toFixed(2), props: { fly: true, neon: n % 2 === 0 }, owner_id: 777777, status: 'listed' });
  const list = [offer(1, 10), offer(2, 20), offer(3, 30)];
  const listCap = cap(1, 'GET', 'https://starpets.gg/api/market/my/offers?offset=0&limit=10', { offers: list, total: 3 });
  const del = cap(2, 'DELETE', 'https://starpets.gg/api/market/offers/9002', null, { status: 204 });
  const post = cap(3, 'POST', 'https://starpets.gg/api/market/offers', { offer: {} }, { status: 201, bodyKind: 'json', body: '{"item_id":"itm_2abc","price":2150}' });
  const r = C.learnEdit(post, [{ cap: listCap, path: 'offers', list }], [del, post]);
  assert.deepEqual(r.edit.steps.map(C.describeTemplate), ['DELETE /api/market/offers/{offer_id}', 'POST /api/market/offers']);
  assert.ok(r.edit.refPaths.includes('item_id') && r.edit.refPaths.includes('offer_id'));
  assert.equal(C.detectScale(r.reqPrice, 21.5), 100);
  const [d, p] = r.edit.steps.map((s) => C.fillTemplate(s, list[0], 9.99, 100));
  assert.equal(d.url, 'https://starpets.gg/api/market/offers/9001');
  assert.equal(p.body, '{"item_id":"itm_1abc","price":999}');
});

test('не учиться на покупке и на смене цены сразу у нескольких', () => {
  const list = [sale(1, 10), sale(2, 20)];
  const lists = [{ cap: cap(1, 'GET', 'https://api.starpets.gg/api/v2/users/me/sales', { items: list }), path: 'items', list }];
  const buy = cap(2, 'POST', 'https://api.starpets.gg/api/v2/cart/add', {}, { bodyKind: 'json', body: '{"sale_id":"s-1","price":10}' });
  assert.equal(C.learnEdit(buy, lists, []), null);
  const batch = cap(3, 'POST', 'https://api.starpets.gg/api/v2/sales/price', {}, { bodyKind: 'json', body: '{"ids":["s-1","s-2"],"price":10}' });
  assert.deepEqual(C.learnEdit(batch, lists, []), { error: 'batch' });
  // чужой список лотов товара (разные продавцы) — не ваш
  const market = [sale(1, 10, { seller: { id: 'u-1' } }), sale(2, 20, { seller: { id: 'u-2' } })];
  const mlists = [{ cap: cap(4, 'GET', 'https://api.starpets.gg/api/v2/products/p-1/sales', { data: market }), path: 'data', list: market }];
  const edit = cap(5, 'PATCH', 'https://api.starpets.gg/api/v2/sales/s-1', {}, { bodyKind: 'json', body: '{"price":"9"}' });
  assert.equal(C.learnEdit(edit, mlists, []), null);
});

test('обучение ценам рынка и расчёт минимума', () => {
  const mine = C.describeMine([sale(1, 50), sale(2, 20), sale(4, 31)]);
  const others = [
    sale(10, 41, { seller: { id: 'u-1' }, product: { id: 'p-1', name: 'Owl' }, item: { id: 'it-10', neon: false, flyable: true, rideable: false } }),
    sale(11, 39.5, { seller: { id: 'u-2' }, product: { id: 'p-1', name: 'Owl' }, item: { id: 'it-11', neon: false, flyable: true, rideable: false } }),
    sale(12, 5, { seller: { id: 'u-3' }, product: { id: 'p-1', name: 'Owl' }, item: { id: 'it-12', neon: true, flyable: true, rideable: false } }),
    sale(13, 1, { seller: { id: 'u-4' }, status: 'sold', product: { id: 'p-1', name: 'Owl' }, item: { id: 'it-13', neon: false, flyable: true, rideable: false } }),
  ];
  const lotsCap = cap(9, 'GET', 'https://api.starpets.gg/api/v2/products/p-1/sales?sort=price_asc', { data: [mine.list[0], ...others] });
  const r = C.learnLots(lotsCap, mine, 'https://api.starpets.gg/api/v2/users/me/sales');
  assert.equal(C.describeTemplate(r.lots), 'GET /api/v2/products/{id}/sales?sort=price_asc');
  assert.deepEqual(r.lots.vars, ['product.id']);
  const m = C.minFromLots(lotsCap.json, r.lots, mine.list[0], mine);
  assert.deepEqual(m, { min: 39.5, count: 2, mineCount: 1, otherVariant: 1 });
  assert.equal(C.fillTemplate(r.lots, mine.list[1], null, 1, true).url, 'https://api.starpets.gg/api/v2/products/p-2/sales?sort=price_asc');
  // список ваших же товаров рынком не считается
  const own = cap(10, 'GET', 'https://api.starpets.gg/api/v2/sellers/p-1/sales', { data: mine.list });
  assert.equal(C.learnLots(own, mine, null), null);
});

test('цены рынка по имени и признакам варианта в параметрах', () => {
  const offer = (n, price, props, owner = 777777) => ({ offer_id: 9000 + n, item_id: `itm_${n}`, name: n < 50 ? 'Frost Dragon' : 'Giraffe', price: String(price), props, owner_id: owner });
  const mine = C.describeMine([offer(1, 120, { fly: true, ride: true, neon: true, mega: false }), offer(51, 33, { fly: false, ride: true, neon: false, mega: false })]);
  const lots = [offer(5, 115, { fly: true, ride: true, neon: true, mega: false }, 1), offer(6, 20, { fly: false, ride: false, neon: false, mega: false }, 2)];
  const c = cap(3, 'GET', 'https://starpets.gg/api/market/catalog/frost-dragon/offers?fly=1&ride=1&neon=1&mega=0&sort=price_asc', { offers: lots });
  const r = C.learnLots(c, mine, 'https://starpets.gg/api/market/my/offers');
  assert.equal(C.describeTemplate(r.lots), 'GET /api/market/catalog/{name}/offers?fly={fly}&ride={ride}&neon={neon}&mega={mega}&sort=price_asc');
  assert.equal(C.fillTemplate(r.lots, mine.list[1], null).url, 'https://starpets.gg/api/market/catalog/giraffe/offers?fly=0&ride=1&neon=0&mega=0&sort=price_asc');
  assert.equal(C.minFromLots(c.json, r.lots, mine.list[0], mine).min, 115);
});

test('новая цена', () => {
  const s = { undercut: 0, floor: 0, maxDrop: 30, raise: false };
  assert.deepEqual(C.planPrice(20, 18.5, s), { action: 'change', target: 18.5, drop: 7.5, note: '', big: false });
  assert.equal(C.planPrice(20, 18.5, { ...s, undercut: 0.01 }).target, 18.49);
  assert.equal(C.planPrice(20, 20, s).action, 'same');
  assert.equal(C.planPrice(20, 25, s).action, 'same');
  assert.equal(C.planPrice(20, 25, { ...s, raise: true }).target, 25);
  assert.equal(C.planPrice(20, null, s).action, 'skip');
  assert.equal(C.planPrice(40, 15, s).big, true);
  assert.equal(C.planPrice(20, 5, { ...s, floor: 8 }).target, 8);
  assert.equal(C.planPrice(0.3 + 0.6, 0.3 + 0.59, s).target, 0.89);
});

test('все страницы списка', async () => {
  const all = Array.from({ length: 23 }, (_, i) => sale(i + 1, i + 1));
  const calls = [];
  const send = async (req) => {
    calls.push(req.url);
    const u = new URL(req.url);
    const page = Number(u.searchParams.get('page'));
    return { status: 200, json: { data: { items: all.slice((page - 1) * 10, page * 10), total: 23 } } };
  };
  const r = await C.fetchAllPages({ method: 'GET', url: 'https://x.gg/api/my/sales?page=2&limit=10', listPath: 'data.items' }, send);
  assert.equal(r.list.length, 23);
  assert.deepEqual(calls.map((u) => new URL(u).searchParams.get('page')), ['1', '2', '3']);
  const off = await C.fetchAllPages({ url: 'https://x.gg/api/my/offers?offset=0&limit=10', listPath: 'offers' }, async (req) => {
    const o = Number(new URL(req.url).searchParams.get('offset'));
    return { status: 200, json: { offers: all.slice(o, o + 10) } };
  });
  assert.equal(off.list.length, 23);
  const fail = await C.fetchAllPages({ url: 'https://x.gg/api/my' }, async () => ({ status: 401, json: null }));
  assert.equal(fail.status, 401);
});

test('отчёт без токенов и почты', () => {
  const t = C.redact('{"access_token":"abc","x":"Bearer qwe.rty","mail":"me@mail.ru"} https://x/api?token=zzz&a=1 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig');
  assert.ok(!/abc|qwe|me@mail|zzz|eyJhbGci/.test(t), t);
});

test('GraphQL и сгруппированные товары', () => {
  const gql = (query, variables) => cap(1, 'POST', 'https://starpets.gg/graphql', {}, { bodyKind: 'json', body: JSON.stringify({ query, variables }) });
  assert.ok(C.isReadLike(gql('query MySales { mySales { id price } }', {})));
  assert.ok(!C.isReadLike(gql('mutation SetPrice($id: ID!, $price: Float!) { setPrice(id: $id, price: $price) { id } }', { id: 's-1', price: 9 })));
  assert.ok(C.isReadLike(cap(2, 'POST', 'https://starpets.gg/api/market/search', {}, { bodyKind: 'json', body: '{"name":"Owl"}' })));
  assert.ok(!C.isReadLike(cap(3, 'POST', 'https://starpets.gg/api/market/offers', {}, { bodyKind: 'json', body: '{"price":1}' })));
  const list = [sale(1, 10), sale(2, 20)];
  const lists = [{ cap: gql('query MySales { me { sales { id price } } }', {}), path: 'data.me.sales', list }];
  const r = C.learnEdit(gql('mutation SetPrice($id: ID!, $price: Float!) { setPrice(id: $id, price: $price) { id } }', { id: 's-2', price: 19.5 }), lists, [], null);
  assert.equal(r.mine.method, 'POST');
  assert.equal(JSON.parse(C.fillTemplate(r.edit.steps[0], list[0], 8.25, 1).body).variables.price, 8.25);
  assert.equal(JSON.parse(C.fillTemplate(r.edit.steps[0], list[0], 8.25, 1).body).variables.id, 's-1');
  assert.ok(C.sameEdit(r.edit, r.edit));
  assert.equal(C.qtyOf({ id: 1, price: 2, quantity: 3 }), 3);
  assert.equal(C.qtyOf({ id: 1, price: 2 }), 1);
});
