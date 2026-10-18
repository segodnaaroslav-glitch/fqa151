/*
 * StarPets Helper — панель на странице сайта.
 *
 * Показывает общую стоимость ваших товаров на продаже и её же минус комиссию,
 * ставит выбранные товары на минимальную цену рынка и показывает в мини-окне,
 * на какую цену изменился каждый товар.
 */
(() => {
  'use strict';
  if (window.top !== window || window.__spxContent) return;
  window.__spxContent = true;

  const C = globalThis.SPX;
  const VERSION = chrome.runtime.getManifest().version;
  const SITE = location.hostname.split('.').slice(-2).join('.');
  const STORE_KEY = `spx:${SITE}`;
  const DEFAULTS = { fee: 20, undercut: 0, floor: 0, maxDrop: 30, raise: false };

  const S = {
    caps: [],             // ответы сайта со списками лотов: {cap, lists}
    muts: [],             // изменяющие запросы сайта (смена цены, снятие...)
    diag: [],             // запросы сайта для отчёта
    ops: {},              // выученное: mine, edit, lots, scale
    settings: { ...DEFAULTS },
    ui: { collapsed: false, hidden: false, left: null, top: null },
    guess: null,          // угаданный список ваших товаров (пока не видели смену цены)
    mine: null,           // C.describeMine(...) + source
    items: [],            // строки панели: {key, o, name, variant, price}
    selected: new Set(),
    market: new Map(),    // key товара -> {min, count, error}
    lotCache: new Map(),  // адрес лотов -> {json, t}
    busy: false,
    status: '',
    flash: '',
    flashT: 0,
    loadError: '',
    verified: false,
    stopped: '',
    editWorked: false,
    editMissed: 0,
  };

  // ---------- хранилище ----------

  const storage = chrome.storage && chrome.storage.local;

  function load() {
    return new Promise((resolve) => {
      try {
        storage.get(STORE_KEY, (v) => resolve((v && v[STORE_KEY]) || {}));
      } catch (e) {
        resolve({});
      }
    });
  }

  function save() {
    try {
      storage.set({ [STORE_KEY]: { ops: S.ops, settings: S.settings, ui: S.ui } });
    } catch (e) {
      // расширение перезагрузили — страницу нужно обновить
    }
  }

  // ---------- связь со страницей ----------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let reqSeq = 0;
  const pending = new Map();
  let loaded = false;
  const early = [];   // запросы сайта, пришедшие до загрузки выученного

  function takeCapture(d) {
    try {
      onCapture(d);
    } catch (err) {
      console.warn('StarPets Helper:', err);
    }
  }

  const parse = (e) => {
    try {
      return typeof e.detail === 'string' ? JSON.parse(e.detail) : null;
    } catch (err) {
      return null;
    }
  };

  document.addEventListener('spx:cap', (e) => {
    const d = parse(e);
    if (!d) return;
    if (loaded) takeCapture(d);
    else early.push(d);
  });

  document.addEventListener('spx:res', (e) => {
    const d = parse(e);
    if (!d || !pending.has(d.id)) return;
    const cb = pending.get(d.id);
    pending.delete(d.id);
    cb(d);
  });

  function request(req, timeout = 30000) {
    return new Promise((resolve) => {
      const id = `${Date.now()}-${++reqSeq}`;
      const timer = setTimeout(() => {
        pending.delete(id);
        resolve({ status: 0, text: '', json: null, error: 'сайт не ответил вовремя' });
      }, timeout);
      pending.set(id, (d) => {
        clearTimeout(timer);
        resolve({ status: d.status, text: d.text || '', json: C.parseJSON(d.text), error: d.error });
      });
      document.dispatchEvent(new CustomEvent('spx:req', { detail: JSON.stringify({ id, ...req }) }));
    });
  }

  /** Повторяет при сбое сети, 429 и 502–504; остальные ответы не повторяет. */
  async function requestRetry(req) {
    let res;
    for (let a = 0; a < 3; a++) {
      res = await request(req);
      if (res.status && res.status !== 429 && !(res.status >= 502 && res.status <= 504)) return res;
      if (a < 2) await sleep(900 * (a + 1));
    }
    return res;
  }

  // ---------- что делает сайт ----------

  function onCapture(d) {
    const cap = {
      seq: d.seq, t: d.t, method: d.method, url: d.url, status: d.status,
      bodyKind: d.bodyKind, body: d.body, contentType: d.contentType, json: C.parseJSON(d.text),
    };
    addDiag(cap, d.text);
    if (!C.isReadLike(cap)) {
      S.muts.push(cap);
      if (S.muts.length > 40) S.muts.shift();
      if (!C.ok(cap.status) || S.busy) return;
      // вы поменяли цену на сайте вручную — запоминаем (или освежаем) как
      if (!learnEditFrom(cap) && S.mine) scheduleRefresh(1500);
      return;
    }
    if (!cap.json || !C.ok(cap.status)) return;
    const lists = C.priceLists(cap.json, false);
    if (!lists.length) return;
    S.caps.push({ cap, lists });
    if (S.caps.length > 80) S.caps.shift();
    const op = mineOp();
    if (op && C.sameEndpoint(cap.url, op.url)) {
      if (!S.busy) scheduleRefresh(600);
    } else if (!S.ops.mine && !S.guess) {
      const g = C.guessMine(cap);
      if (g) {
        S.guess = g;
        setMine(g.list, 'guess');
        scheduleRefresh(300);
      }
    }
    if (S.mine && !S.ops.lots) learnLotsFrom(cap);
  }

  const mineOp = () => S.ops.mine || (S.guess && S.guess.op) || null;

  function allLists() {
    const out = [];
    for (const c of S.caps) for (const l of c.lists) out.push({ cap: c.cap, path: l.path, list: l.list });
    const op = mineOp();
    if (S.mine && op) out.push({ cap: { ...op, seq: Number.MAX_SAFE_INTEGER }, path: op.listPath, list: S.mine.list });
    return out;
  }

  function learnEditFrom(cap) {
    const r = C.learnEdit(cap, allLists(), S.muts, mineOp() && mineOp().url);
    if (!r) return false;
    if (r.error === 'batch') {
      flash('Чтобы я запомнил смену цены, поменяйте цену у одного товара, а не у нескольких сразу.');
      return false;
    }
    const before = S.ops.edit;
    // уже выученную и проверенную смену цены заменяем только таким же запросом (свежим)
    if (before && S.ops.scale && !C.sameEdit(before, r.edit)) return false;
    const mineChanged = !S.ops.mine || !C.sameEndpoint(S.ops.mine.url, r.mine.url) || S.ops.mine.listPath !== r.mine.listPath;
    const prevGuess = S.guess && S.guess.op;
    // единицы цены те же, если сайт меняет цену тем же запросом; иначе проверим заново
    if (!(before && C.sameEdit(before, r.edit))) S.ops.scale = null;
    S.ops.edit = r.edit;
    S.ops.mine = r.mine;
    if (mineChanged && S.ops.lots && !(prevGuess && C.sameEndpoint(prevGuess.url, r.mine.url))) delete S.ops.lots;
    S.guess = null;
    S.verified = false;
    S.stopped = '';
    S.editWorked = false;
    S.editMissed = 0;
    save();
    if (!before) flash(`✓ Запомнил, как сайт меняет цену: ${r.edit.steps.map(C.describeTemplate).join(' → ')}`);
    setTimeout(async () => {
      await refresh();
      const o = S.mine && S.mine.list.find((x) => C.flatten(x, 4).some(([p, v]) => C.isIdKey(C.leafKey(p)) && r.ids.includes(String(v))));
      const p = o && C.listPrice(o);
      const scale = C.detectScale(r.reqPrice, p && p.value);
      if (scale) {
        S.ops.scale = scale;
        save();
      }
      if (!S.ops.lots) for (const c of S.caps) if (!S.ops.lots) learnLotsFrom(c.cap);
      render();
    }, 1500);
    render();
    return true;
  }

  function learnLotsFrom(cap) {
    const r = C.learnLots(cap, S.mine, mineOp() && mineOp().url);
    if (!r) return;
    S.ops.lots = r.lots;
    S.market.clear();
    S.lotCache.clear();
    save();
    flash(`✓ Запомнил, где смотреть цены других продавцов: ${C.describeTemplate(r.lots)}`);
    render();
  }

  // ---------- ваши товары ----------

  function setMine(list, source) {
    const d = C.describeMine(list, { refPaths: S.ops.edit && S.ops.edit.refPaths });
    S.mine = { ...d, source };
    S.items = d.list.map((o) => ({
      key: C.itemKey(o, d),
      o,
      name: C.itemName(o) || 'Без названия',
      variant: C.variantLabel(o),
      price: C.listPrice(o).value,
      qty: C.qtyOf(o),
    }));
    const keys = new Set(S.items.map((i) => i.key));
    for (const k of [...S.selected]) if (!keys.has(k)) S.selected.delete(k);
    if (!S.ops.lots) for (const c of S.caps) if (!S.ops.lots) learnLotsFrom(c.cap);
    render();
  }

  let refreshTimer = null;
  let refreshing = null;

  function scheduleRefresh(ms) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh(), ms);
  }

  async function refresh() {
    const op = mineOp();
    if (!op) return false;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const res = await C.fetchAllPages(op, requestRetry);
      if (!res.list) {
        S.loadError = res.status === 401 || res.status === 403
          ? 'Сайт не пустил к вашим товарам — войдите в аккаунт и обновите страницу.'
          : `Не смог загрузить ваши товары: ${res.error}`;
        render();
        return false;
      }
      S.loadError = '';
      setMine(res.list, S.ops.mine ? 'learned' : 'guess');
      return true;
    })();
    try {
      return await refreshing;
    } finally {
      refreshing = null;
    }
  }

  function findSame(it) {
    const byKey = S.items.find((i) => i.key === it.key);
    if (byKey || !S.mine) return byKey || null;
    const ids = new Set(S.mine.ownPaths.map((p) => C.getPath(it.o, p)).filter((v) => v !== undefined && v !== null).map(String));
    return S.items.find((i) => S.mine.ownPaths.some((p) => {
      const v = C.getPath(i.o, p);
      return v !== undefined && v !== null && ids.has(String(v));
    })) || null;
  }

  // ---------- цены рынка ----------

  async function marketFor(it, fresh) {
    const lots = S.ops.lots;
    if (!lots) {
      const f = C.marketMinField(it.o);
      return f !== null ? { min: f, count: null, source: 'field' } : { error: 'ещё не знаю, где смотреть цены рынка' };
    }
    let req;
    try {
      req = C.fillTemplate(lots, it.o, null, 1, true);
    } catch (e) {
      return { error: e.message };
    }
    let cached = S.lotCache.get(req.url);
    if (!cached || (fresh && Date.now() - cached.t > 5000) || Date.now() - cached.t > 60000) {
      const res = await requestRetry(req);
      if (!C.ok(res.status)) return { error: res.status ? `рынок ответил ${res.status}` : `рынок не ответил: ${res.error}` };
      cached = { json: res.json, t: Date.now() };
      S.lotCache.set(req.url, cached);
    }
    return C.minFromLots(cached.json, lots, it.o, S.mine);
  }

  async function pool(list, n, fn) {
    let i = 0;
    const worker = async () => {
      while (i < list.length) {
        const x = list[i++];
        await fn(x);
      }
    };
    await Promise.all(Array.from({ length: Math.min(n, list.length) }, worker));
  }

  async function checkMarket() {
    if (S.busy) return;
    if (!S.items.length) return flash('Пока не вижу ваших товаров.');
    S.busy = true;
    render();
    let n = 0;
    try {
      await pool(S.items, 3, async (it) => {
        setStatus(`Смотрю цены рынка… ${++n}/${S.items.length}`);
        S.market.set(it.key, await marketFor(it, true));
      });
      const above = S.items.filter((it) => aboveMarket(it)).length;
      flash(above ? `Дороже минимальной цены рынка: ${above} шт. Нажмите «выбрать дороже рынка».` : 'Все ваши товары уже стоят по минимальной цене рынка или дешевле.');
    } finally {
      S.busy = false;
      setStatus('');
      render();
    }
  }

  function aboveMarket(it) {
    const m = S.market.get(it.key);
    if (!m || m.min === null || m.min === undefined) return false;
    const p = C.planPrice(it.price, m.min, S.settings);
    return p.action === 'change' && p.target < it.price;
  }

  // ---------- смена цены ----------

  async function editPrice(it, target) {
    const scale = S.ops.scale || 1;
    for (const step of S.ops.edit.steps) {
      const req = C.fillTemplate(step, it.o, target, scale);
      const res = await requestRetry(req);
      if (C.ok(res.status)) S.editWorked = true;
      else if ((res.status === 404 || res.status === 405) && !S.editWorked && ++S.editMissed >= 3) {
        // запрос ни разу не сработал и трижды «не найден» — сайт поменял API
        delete S.ops.edit;
        save();
        S.stopped = 'сайт изменил запрос смены цены — покажите смену цены ещё раз (поменяйте цену одного товара вручную).';
      }
      if (!C.ok(res.status)) {
        const why = res.status ? `сайт ответил ${res.status}` : `нет связи с сайтом (${res.error})`;
        throw new Error(`${why}${res.text ? `: ${shortText(res.text)}` : ''}`);
      }
    }
  }

  /** Первая смена за сессию: проверяем на сайте, что цена встала именно та. */
  async function verifyFirst(plan) {
    for (let a = 0; a < 5; a++) {
      await sleep(900);
      await refresh();
      const now = findSame(plan.it);
      if (!now) continue;
      if (Math.abs(now.price - plan.target) < 0.005) return '';
      if (Math.abs(now.price - plan.it.price) < 0.005) continue;   // сайт ещё не обновил цену
      delete S.ops.edit;
      save();
      return `проверка не прошла: ставил ${fmt(plan.target)}, а на сайте ${fmt(now.price)}. Смена цен остановлена — `
        + 'проверьте этот товар на сайте и покажите смену цены ещё раз (поменяйте цену одного товара вручную).';
    }
    return 'проверка не прошла: после смены цена на сайте не изменилась. Смена цен остановлена.';
  }

  async function applyMin(keys) {
    if (S.busy) return;
    if (!S.ops.edit || !S.ops.lots || !S.ops.scale) {
      await showModal({ title: 'Нужно один раз показать', html: setupHtml(true), buttons: [{ label: 'Понятно', primary: true }] });
      return;
    }
    if (S.stopped) {
      await showModal({ title: 'Смена цен остановлена', html: esc(S.stopped), buttons: [{ label: 'OK', primary: true }] });
      return;
    }
    if (!keys.length) return flash('Отметьте товары галочками.');
    S.busy = true;
    render();
    const plans = [];
    try {
      setStatus('Обновляю ваши товары…');
      if (!(await refresh())) throw new Error(S.loadError || 'не смог загрузить ваши товары');
      const items = keys.map((k) => S.items.find((i) => i.key === k)).filter(Boolean);
      if (!items.length) throw new Error('выбранных товаров больше нет среди ваших товаров на продаже');
      let n = 0;
      await pool(items, 3, async (it) => {
        setStatus(`Смотрю цены рынка… ${++n}/${items.length}`);
        S.market.set(it.key, await marketFor(it, true));
      });
      for (const it of items) {
        const m = S.market.get(it.key);
        if (m.error) plans.push({ it, action: 'error', reason: m.error });
        else plans.push({ it, m, ...C.planPrice(it.price, m.min, S.settings) });
      }
      const big = plans.filter((p) => p.action === 'change' && p.big);
      if (big.length) {
        const rows = big.map((p) => `<div class="r"><span>${esc(label(p.it))}</span><b>${fmt(p.it.price)} → ${fmt(p.target)} (−${Math.round(p.drop)}%)</b></div>`).join('');
        const ans = await showModal({
          title: 'Цена сильно упадёт',
          html: `<p>У этих товаров цена упадёт больше чем на ${S.settings.maxDrop}%:</p>${rows}`,
          buttons: [{ label: 'Всё равно поставить', value: 'yes' }, { label: 'Не трогать их', value: 'no', primary: true }],
        });
        if (ans !== 'yes') for (const p of big) Object.assign(p, { action: 'skip', reason: `не тронул: цена упала бы на ${Math.round(p.drop)}%` });
      }
      const todo = plans.filter((p) => p.action === 'change');
      let i = 0;
      for (const p of todo) {
        if (S.stopped) {
          Object.assign(p, { action: 'skip', reason: 'не тронул: смена цен остановлена' });
          continue;
        }
        setStatus(`Меняю цены… ${++i}/${todo.length}`);
        try {
          await editPrice(p.it, p.target);
          p.done = true;
        } catch (e) {
          Object.assign(p, { action: 'error', reason: e.message });
          continue;
        }
        if (!S.verified) {
          const problem = await verifyFirst(p);
          if (problem) {
            S.stopped = problem;
            Object.assign(p, { action: 'error', reason: problem });
          } else {
            S.verified = true;
          }
        }
        await sleep(250);
      }
      setStatus('Проверяю новые цены…');
      await refresh();
      for (const p of todo) {
        if (!p.done) continue;
        const now = findSame(p.it);
        p.after = now ? now.price : null;
      }
    } catch (e) {
      plans.length = 0;
      await showModal({ title: 'Не получилось', html: esc(e.message), buttons: [{ label: 'OK', primary: true }] });
    } finally {
      S.busy = false;
      setStatus('');
      render();
    }
    if (plans.length) {
      for (const p of plans) if (p.done) S.selected.delete(p.it.key);
      render();
      await showResults(plans);
    }
  }

  // ---------- оформление ----------

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const shortText = (t) => String(t).replace(/\s+/g, ' ').slice(0, 160);
  const cur = () => (S.mine && S.mine.currency) || '';
  const fmt = (n) => `${Number(n).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${cur() ? ` ${cur()}` : ''}`;
  const label = (it) => `${it.name}${it.variant ? ` ${it.variant}` : ''}`;
  const feeLabel = () => `Минус ${String(S.settings.fee).replace('.', ',')}%`;
  const net = (sum) => sum * (1 - (Number(S.settings.fee) || 0) / 100);
  const sumOf = (items) => items.reduce((a, it) => a + it.price * it.qty, 0);
  const pieces = (items) => items.reduce((a, it) => a + it.qty, 0);

  function setStatus(s) {
    S.status = s;
    renderStatus();
  }

  function flash(s) {
    S.flash = s;
    S.flashT = Date.now();
    renderStatus();
    setTimeout(() => {
      if (Date.now() - S.flashT >= 9000) {
        S.flash = '';
        renderStatus();
      }
    }, 9100);
  }

  function hint() {
    if (S.loadError) return S.loadError;
    if (!S.mine) return 'Откройте на сайте страницу с вашими товарами на продаже — я посчитаю их стоимость.';
    if (!S.ops.edit) return 'Чтобы менять цены, один раз измените вручную цену любого своего товара на сайте.';
    if (!S.ops.scale) return 'Измените вручную цену одного товара ещё раз — проверю, в каких единицах сайт передаёт цену.';
    if (!S.ops.lots) return 'Чтобы видеть цены рынка, откройте на сайте страницу любого своего товара, у которого есть другие продавцы.';
    return '';
  }

  function setupHtml(intro) {
    const step = (done, warn, title, text) => `<div class="step ${done ? 'done' : warn ? 'warn' : ''}"><span class="mark">${done ? '✓' : warn ? '!' : '○'}</span><div><b>${title}</b><div class="muted">${text}</div></div></div>`;
    const mine = S.mine;
    return `${intro ? '<p>Расширение учится у самого сайта. Сделайте на сайте по одному разу:</p>' : ''}
      ${step(S.ops.mine && mine, mine && mine.source === 'guess', 'Ваши товары на продаже',
    S.ops.mine && mine ? `вижу ${mine.list.length} шт.` : mine ? `нашёл ${mine.list.length} шт. — уточнится после шага 2` : 'откройте страницу с вашими товарами на продаже')}
      ${step(S.ops.edit && S.ops.scale, S.ops.edit && !S.ops.scale, 'Смена цены',
    S.ops.edit ? (S.ops.scale ? esc(S.ops.edit.steps.map(C.describeTemplate).join(' → ')) : 'измените цену одного товара ещё раз') : 'измените вручную цену одного своего товара (можно на ту же)')}
      ${step(S.ops.lots, false, 'Цены других продавцов',
    S.ops.lots ? esc(C.describeTemplate(S.ops.lots)) : 'откройте страницу своего товара, где видно других продавцов')}`;
  }

  // ---------- панель ----------

  let host = null;
  let root = null;
  const $ = (sel) => root.querySelector(sel);

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    .panel, .pill, .modal { font: 13px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #ecebf5; }
    .panel { position: fixed; z-index: 2147483000; width: 370px; max-height: calc(100vh - 40px); display: flex; flex-direction: column;
      background: #1b1a26; border: 1px solid #343249; border-radius: 12px; box-shadow: 0 10px 34px rgba(0,0,0,.45); overflow: hidden; }
    .head { display: flex; align-items: center; gap: 8px; padding: 9px 10px 9px 12px; background: #23213a; cursor: move; user-select: none; }
    .head b { font-size: 13px; letter-spacing: .2px; }
    .logo { color: #ffcf4a; font-size: 15px; }
    .sp { flex: 1; }
    button { font: inherit; color: inherit; cursor: pointer; }
    button:disabled { opacity: .5; cursor: default; }
    .ic { background: transparent; border: 0; width: 26px; height: 26px; border-radius: 6px; font-size: 15px; color: #b9b6d3; }
    .ic:hover { background: #33304d; color: #fff; }
    .sum { padding: 12px 14px 8px; display: grid; gap: 4px; }
    .row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
    .row span { color: #b9b6d3; }
    .row b { font-size: 18px; font-variant-numeric: tabular-nums; }
    .green { color: #46d39a; }
    .muted, .sub { color: #8e8aab; font-size: 12px; }
    .msg { margin: 0 12px 8px; padding: 7px 9px; border-radius: 8px; background: #2a2840; color: #d9d6f2; font-size: 12px; }
    .msg:empty { display: none; }
    .msg.err { background: #3d2230; color: #ffc2cf; }
    .actions { padding: 0 12px 8px; display: grid; gap: 6px; }
    .primary { background: #7b5cff; border: 0; border-radius: 8px; padding: 9px 10px; font-weight: 600; color: #fff; }
    .primary:hover:not(:disabled) { background: #8d72ff; }
    .row2 { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; color: #b9b6d3; }
    .row2 label { display: flex; align-items: center; gap: 5px; cursor: pointer; }
    .link { background: none; border: 0; padding: 0; color: #a995ff; font-size: 12px; text-decoration: underline dotted; }
    .list { overflow-y: auto; border-top: 1px solid #2d2b42; min-height: 40px; flex: 1 1 auto; }
    .empty { padding: 14px; color: #8e8aab; text-align: center; font-size: 12px; }
    .item { display: grid; grid-template-columns: 18px 1fr auto auto; align-items: center; gap: 8px; padding: 7px 12px; border-bottom: 1px solid #25233a; }
    .item.sel { background: #262342; }
    .item input { margin: 0; cursor: pointer; accent-color: #7b5cff; }
    .nm { min-width: 0; }
    .name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .var { color: #ffcf4a; font-size: 11px; font-weight: 700; margin-left: 4px; }
    .mk { font-size: 11px; color: #8e8aab; }
    .mk.hi { color: #ff9a8a; }
    .mk.okm { color: #46d39a; }
    .pr { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
    .mini { background: #2f2c4a; border: 0; border-radius: 6px; padding: 4px 7px; font-size: 11px; color: #d9d6f2; }
    .mini:hover:not(:disabled) { background: #3d3961; }
    details { border-top: 1px solid #2d2b42; padding: 8px 12px 10px; }
    summary { cursor: pointer; color: #b9b6d3; font-size: 12px; }
    .step { display: flex; gap: 8px; margin: 8px 0; font-size: 12px; }
    .step .mark { width: 18px; height: 18px; flex: none; border-radius: 50%; display: grid; place-items: center; background: #34314d; font-size: 11px; }
    .step.done .mark { background: #1f5a43; color: #7ff0bf; }
    .step.warn .mark { background: #5a4a1f; color: #ffd36e; }
    .grid { display: grid; grid-template-columns: 1fr 90px; gap: 6px 10px; align-items: center; margin-top: 10px; font-size: 12px; color: #b9b6d3; }
    .grid input[type=text] { width: 100%; background: #12111b; border: 1px solid #3a3754; color: #fff; border-radius: 6px; padding: 4px 6px; font: inherit; text-align: right; }
    .chk { display: flex; gap: 6px; align-items: flex-start; margin-top: 8px; font-size: 12px; color: #b9b6d3; }
    .btns { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
    .btn2 { background: #2f2c4a; border: 0; border-radius: 6px; padding: 6px 9px; font-size: 12px; }
    .pill { position: fixed; z-index: 2147483000; right: 16px; bottom: 16px; display: flex; align-items: center; gap: 8px; padding: 8px 12px;
      background: #1b1a26; border: 1px solid #343249; border-radius: 999px; box-shadow: 0 6px 20px rgba(0,0,0,.4); cursor: pointer; font-weight: 600; }
    .pill .green { font-weight: 600; }
    .wrap { position: fixed; inset: 0; z-index: 2147483001; background: rgba(8,7,14,.55); display: grid; place-items: center; padding: 16px; }
    .modal { width: min(440px, 100%); max-height: calc(100vh - 32px); display: flex; flex-direction: column; background: #1f1d2e; border: 1px solid #3a3754;
      border-radius: 14px; box-shadow: 0 16px 48px rgba(0,0,0,.55); overflow: hidden; }
    .modal h3 { margin: 0; padding: 14px 16px 10px; font-size: 15px; }
    .mbody { padding: 0 16px 6px; overflow-y: auto; }
    .mbody p { margin: 0 0 8px; color: #c9c6e3; }
    .r { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid #2d2b42; }
    .r:last-child { border-bottom: 0; }
    .r b { white-space: nowrap; font-variant-numeric: tabular-nums; }
    .r .why { color: #8e8aab; font-size: 12px; text-align: right; }
    .r.ok b { color: #46d39a; }
    .r.bad .why { color: #ff9a8a; }
    .tot { margin: 6px 0 8px; padding: 8px 10px; background: #2a2840; border-radius: 8px; display: grid; gap: 2px; font-size: 12px; }
    .mfoot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 16px 14px; }
    .mfoot button { border: 0; border-radius: 8px; padding: 8px 14px; background: #2f2c4a; }
    .mfoot button.primary { background: #7b5cff; }
  `;

  function mount() {
    if (host) return;
    host = document.createElement('div');
    host.id = 'starpets-helper';
    root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `<style>${CSS}</style>
      <div class="panel" hidden>
        <div class="head" data-drag>
          <span class="logo">★</span><b>StarPets Helper</b><span class="sp"></span>
          <button class="ic" data-act="refresh" title="Обновить список">⟳</button>
          <button class="ic" data-act="collapse" title="Свернуть">–</button>
        </div>
        <div class="sum">
          <div class="row"><span>Общая стоимость</span><b data-f="total">—</b></div>
          <div class="row"><span data-f="feeLabel"></span><b class="green" data-f="net">—</b></div>
          <div class="sub" data-f="counts"></div>
        </div>
        <div class="msg" data-f="msg"></div>
        <div class="actions">
          <button class="primary" data-act="apply-selected">Поставить выбранные на мин. цену</button>
          <div class="row2">
            <label><input type="checkbox" data-act="select-all"> все</label>
            <button class="link" data-act="select-above">выбрать дороже рынка</button>
            <button class="link" data-act="check">проверить мин. цены</button>
          </div>
        </div>
        <div class="list" data-f="list"></div>
        <details data-f="setup">
          <summary>Настройка</summary>
          <div data-f="steps"></div>
          <div class="grid">
            <span>Комиссия (второй счёт), %</span><input type="text" data-set="fee">
            <span>Ставить ниже минимума на</span><input type="text" data-set="undercut">
            <span>Не ставить цену ниже</span><input type="text" data-set="floor">
            <span>Спрашивать, если цена падает больше чем на, %</span><input type="text" data-set="maxDrop">
          </div>
          <label class="chk"><input type="checkbox" data-set="raise"> Поднимать цену до минимума других продавцов, если мой товар и так дешевле всех</label>
          <div class="btns">
            <button class="btn2" data-act="report">Скачать отчёт</button>
            <button class="btn2" data-act="reset">Сбросить обучение</button>
          </div>
          <div class="muted" style="margin-top:8px">Версия ${esc(VERSION)}</div>
        </details>
      </div>
      <div class="pill" data-act="expand" hidden></div>
      <div data-f="modal"></div>`;
    document.documentElement.appendChild(host);
    root.addEventListener('click', onClick);
    root.addEventListener('change', onChange);
    setupDrag();
    render();
  }

  function render() {
    if (!root) return;
    const panel = $('.panel');
    const pill = $('.pill');
    panel.hidden = S.ui.hidden || S.ui.collapsed;
    pill.hidden = S.ui.hidden || !S.ui.collapsed;
    if (S.ui.left !== null && S.ui.top !== null) {
      panel.style.left = `${Math.max(0, Math.min(S.ui.left, window.innerWidth - 120))}px`;
      panel.style.top = `${Math.max(0, Math.min(S.ui.top, window.innerHeight - 60))}px`;
      panel.style.right = 'auto';
    } else {
      panel.style.right = '16px';
      panel.style.top = '72px';
    }

    const total = sumOf(S.items);
    const selSum = sumOf(S.items.filter((it) => S.selected.has(it.key)));
    $('[data-f=total]').textContent = S.mine ? fmt(total) : '—';
    $('[data-f=net]').textContent = S.mine ? fmt(net(total)) : '—';
    $('[data-f=feeLabel]').textContent = feeLabel();
    $('[data-f=counts]').textContent = S.mine
      ? `Товаров: ${pieces(S.items)}${S.selected.size ? ` · выбрано ${S.selected.size} на ${fmt(selSum)} (${feeLabel().toLowerCase()}: ${fmt(net(selSum))})` : ''}`
      : '';
    pill.innerHTML = `<span class="logo">★</span><span>${S.mine ? fmt(total) : 'StarPets Helper'}</span>${S.mine ? `<span class="green">${fmt(net(total))}</span>` : ''}`;

    const all = $('[data-act=select-all]');
    all.checked = S.items.length > 0 && S.selected.size === S.items.length;
    all.indeterminate = S.selected.size > 0 && S.selected.size < S.items.length;
    for (const b of root.querySelectorAll('.actions button, .mini, [data-act=refresh]')) b.disabled = S.busy;
    $('[data-act=apply-selected]').textContent = S.selected.size
      ? `Поставить выбранные (${S.selected.size}) на мин. цену`
      : 'Поставить выбранные на мин. цену';

    renderList();
    renderStatus();
    $('[data-f=steps]').innerHTML = setupHtml(false);
    for (const inp of root.querySelectorAll('[data-set]')) {
      const k = inp.dataset.set;
      if (inp.type === 'checkbox') inp.checked = !!S.settings[k];
      else if (root.activeElement !== inp) inp.value = String(S.settings[k]).replace('.', ',');
    }
  }

  function renderList() {
    const box = $('[data-f=list]');
    if (!S.items.length) {
      box.innerHTML = `<div class="empty">${S.mine ? 'Товаров на продаже нет.' : 'Здесь появятся ваши товары на продаже.'}</div>`;
      return;
    }
    const scroll = box.scrollTop;
    box.innerHTML = S.items.map((it) => {
      const m = S.market.get(it.key);
      let mk = '';
      if (m && m.error) mk = `<div class="mk">рынок: ${esc(m.error)}</div>`;
      else if (m && m.min !== null && m.min !== undefined) {
        const cls = aboveMarket(it) ? 'hi' : 'okm';
        mk = `<div class="mk ${cls}">мин. на рынке: ${fmt(m.min)}</div>`;
      } else if (m) mk = '<div class="mk">других продавцов нет</div>';
      const sel = S.selected.has(it.key);
      return `<div class="item${sel ? ' sel' : ''}" data-key="${esc(it.key)}">
        <input type="checkbox" data-act="toggle" ${sel ? 'checked' : ''}>
        <div class="nm"><div class="name" title="${esc(label(it))}">${esc(it.name)}${it.variant ? `<span class="var">${esc(it.variant)}</span>` : ''}</div>${mk}</div>
        <div class="pr">${it.qty > 1 ? `<span class="muted">×${it.qty}</span> ` : ''}${fmt(it.price)}</div>
        <button class="mini" data-act="apply-one" title="Поставить этот товар на минимальную цену"${S.busy ? ' disabled' : ''}>мин.</button>
      </div>`;
    }).join('');
    box.scrollTop = scroll;
  }

  function renderStatus() {
    if (!root) return;
    const box = $('[data-f=msg]');
    const text = S.status || S.flash || hint();
    box.textContent = text;
    box.classList.toggle('err', !S.status && !S.flash && !!S.loadError);
  }

  function itemKeyOf(el) {
    const row = el.closest('.item');
    return row ? row.dataset.key : null;
  }

  async function onClick(e) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    if (act === 'toggle') {
      const k = itemKeyOf(el);
      if (S.selected.has(k)) S.selected.delete(k);
      else S.selected.add(k);
      render();
    } else if (act === 'select-all') {
      if (S.selected.size === S.items.length) S.selected.clear();
      else for (const it of S.items) S.selected.add(it.key);
      render();
    } else if (act === 'select-above') {
      const above = S.items.filter(aboveMarket);
      if (!S.market.size) return flash('Сначала нажмите «проверить мин. цены».');
      S.selected = new Set(above.map((it) => it.key));
      render();
      if (!above.length) flash('Товаров дороже рынка нет.');
    } else if (act === 'check') {
      await checkMarket();
    } else if (act === 'apply-selected') {
      await applyMin([...S.selected]);
    } else if (act === 'apply-one') {
      await applyMin([itemKeyOf(el)]);
    } else if (act === 'refresh') {
      if (!mineOp()) return flash(hint());
      S.lotCache.clear();
      setStatus('Обновляю…');
      await refresh();
      setStatus('');
    } else if (act === 'collapse') {
      S.ui.collapsed = true;
      save();
      render();
    } else if (act === 'expand') {
      S.ui.collapsed = false;
      save();
      render();
    } else if (act === 'report') {
      downloadReport();
    } else if (act === 'reset') {
      const ans = await showModal({
        title: 'Сбросить обучение?',
        html: '<p>Расширение забудет, как сайт показывает ваши товары, меняет цену и показывает цены рынка. Потом нужно будет показать ещё раз.</p>',
        buttons: [{ label: 'Отмена' }, { label: 'Сбросить', value: 'yes', primary: true }],
      });
      if (ans === 'yes') {
        S.ops = {};
        S.guess = null;
        S.mine = null;
        S.items = [];
        S.selected.clear();
        S.market.clear();
        S.lotCache.clear();
        S.verified = false;
        S.stopped = '';
        save();
        render();
      }
    }
  }

  function onChange(e) {
    const inp = e.target.closest('[data-set]');
    if (!inp) return;
    const k = inp.dataset.set;
    if (inp.type === 'checkbox') S.settings[k] = inp.checked;
    else {
      const v = C.toPrice(inp.value);
      S.settings[k] = v !== null && v >= 0 && !(k === 'fee' && v >= 100) ? v : DEFAULTS[k];
    }
    save();
    render();
  }

  function setupDrag() {
    const head = $('[data-drag]');
    const panel = $('.panel');
    head.addEventListener('mousedown', (e) => {
      if (e.target.closest('button')) return;
      const r = panel.getBoundingClientRect();
      const dx = e.clientX - r.left;
      const dy = e.clientY - r.top;
      const move = (ev) => {
        S.ui.left = Math.max(0, Math.min(ev.clientX - dx, window.innerWidth - 120));
        S.ui.top = Math.max(0, Math.min(ev.clientY - dy, window.innerHeight - 60));
        panel.style.left = `${S.ui.left}px`;
        panel.style.top = `${S.ui.top}px`;
        panel.style.right = 'auto';
      };
      const up = () => {
        window.removeEventListener('mousemove', move, true);
        window.removeEventListener('mouseup', up, true);
        save();
      };
      window.addEventListener('mousemove', move, true);
      window.addEventListener('mouseup', up, true);
      e.preventDefault();
    });
  }

  // ---------- мини-окна ----------

  function showModal({ title, html, buttons }) {
    return new Promise((resolve) => {
      const box = $('[data-f=modal]');
      const btns = buttons || [{ label: 'OK', primary: true }];
      box.innerHTML = `<div class="wrap"><div class="modal" role="dialog" aria-label="${esc(title)}">
        <h3>${esc(title)}</h3><div class="mbody">${html}</div>
        <div class="mfoot">${btns.map((b, i) => `<button data-i="${i}" class="${b.primary ? 'primary' : ''}">${esc(b.label)}</button>`).join('')}</div>
      </div></div>`;
      const done = (v) => {
        box.innerHTML = '';
        resolve(v);
      };
      box.querySelector('.wrap').addEventListener('click', (e) => {
        const b = e.target.closest('button[data-i]');
        if (b) done(btns[+b.dataset.i].value);
        else if (e.target.classList.contains('wrap')) done(undefined);
      });
      const primary = box.querySelector('button.primary') || box.querySelector('button');
      if (primary) primary.focus();
    });
  }

  function showResults(plans) {
    const changed = plans.filter((p) => p.done && p.action === 'change');
    const same = plans.filter((p) => p.action === 'same');
    const other = plans.filter((p) => !p.done && p.action !== 'same');
    const row = (p) => {
      if (p.done && p.action === 'change') {
        const shown = p.after !== null && p.after !== undefined ? p.after : p.target;
        const warn = p.after !== null && p.after !== undefined && Math.abs(p.after - p.target) >= 0.005;
        return `<div class="r ${warn ? 'bad' : 'ok'}"><span>${esc(label(p.it))}${p.note ? `<div class="why">${esc(p.note)}</div>` : ''}${warn ? `<div class="why">ставил ${fmt(p.target)}, на сайте сейчас ${fmt(shown)}</div>` : ''}</span><b>${fmt(p.it.price)} → ${fmt(shown)}</b></div>`;
      }
      if (p.action === 'same') return `<div class="r"><span>${esc(label(p.it))}</span><span class="why">${esc(p.reason)}<br>${fmt(p.it.price)}</span></div>`;
      return `<div class="r bad"><span>${esc(label(p.it))}</span><span class="why">${esc(p.reason || 'не изменён')}</span></div>`;
    };
    const total = sumOf(S.items);
    const title = changed.length === 1 && plans.length === 1
      ? 'Цена изменена'
      : changed.length ? `Цены изменены: ${changed.length}` : 'Цены не изменились';
    const html = `${[...changed, ...other, ...same].map(row).join('')}
      <div class="tot">
        <div class="row"><span>Изменено / без изменений / не вышло</span><b style="font-size:13px">${changed.length} / ${same.length} / ${other.length}</b></div>
        <div class="row"><span>Общая стоимость теперь</span><b style="font-size:13px">${fmt(total)}</b></div>
        <div class="row"><span>${esc(feeLabel())}</span><b class="green" style="font-size:13px">${fmt(net(total))}</b></div>
      </div>`;
    return showModal({ title, html, buttons: [{ label: 'OK', primary: true }] });
  }

  // ---------- отчёт ----------

  function addDiag(cap, text) {
    S.diag.push({
      time: new Date(cap.t || Date.now()).toISOString(),
      method: cap.method,
      url: C.redact(cap.url),
      status: cap.status,
      bodyKind: cap.bodyKind,
      body: cap.body ? C.redact(String(cap.body)).slice(0, 2000) : null,
      response: text ? C.redact(text).slice(0, 4000) : '',
    });
    if (S.diag.length > 300) S.diag.shift();
  }

  function shape(o, depth = 0) {
    if (Array.isArray(o)) return o.length ? [shape(o[0], depth + 1)] : [];
    if (C.isObj(o)) {
      if (depth > 4) return '{…}';
      const out = {};
      for (const k of Object.keys(o)) out[k] = shape(o[k], depth + 1);
      return out;
    }
    if (typeof o === 'string') return o.length > 40 ? `${o.slice(0, 40)}…` : o;
    return o;
  }

  function downloadReport() {
    const report = {
      version: VERSION,
      site: location.host,
      page: location.pathname,
      time: new Date().toISOString(),
      settings: S.settings,
      learned: S.ops,
      mine: S.mine && {
        source: S.mine.source,
        count: S.mine.list.length,
        skipped: S.mine.skipped,
        idPaths: S.mine.idPaths,
        ownPaths: S.mine.ownPaths,
        keyPath: S.mine.keyPath,
        hasUid: S.mine.uid !== null,
        sample: shape(S.mine.list[0]),
      },
      guess: S.guess && S.guess.op,
      loadError: S.loadError,
      stopped: S.stopped,
      market: S.items.slice(0, 50).map((it) => ({ name: label(it), price: it.price, market: S.market.get(it.key) || null })),
      requests: S.diag,
    };
    const blob = new Blob([C.redact(JSON.stringify(report, null, 2))], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `starpets-helper-report-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json`;
    root.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  // ---------- запуск ----------

  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.spx === 'toggle') {
        S.ui.hidden = !S.ui.hidden;
        if (!S.ui.hidden) S.ui.collapsed = false;
        save();
        render();
      }
    });
  } catch (e) {
    // нет кнопки расширения — не страшно
  }

  load().then((saved) => {
    S.ops = saved.ops || {};
    S.settings = { ...DEFAULTS, ...(saved.settings || {}) };
    S.ui = { ...S.ui, ...(saved.ui || {}) };
    loaded = true;
    for (const d of early.splice(0)) takeCapture(d);
    const start = () => {
      mount();
      // список уже известен — покажем итоги сразу, когда сайт отправит первые запросы
      if (S.ops.mine) setTimeout(() => refresh(), 1500);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
    else start();
  });
})();
