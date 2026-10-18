/*
 * StarPets Helper — логика без DOM: разбор ответов сайта, обучение по его
 * запросам, шаблоны запросов и расчёт цены. Этот же файл подключают тесты (node).
 *
 * Адреса API StarPets в расширении не зашиты. Расширение смотрит, какие запросы
 * делает сам сайт, когда вы:
 *   - открываете страницу своих товаров на продаже (так находится список товаров);
 *   - меняете цену одного товара вручную (так находится запрос смены цены);
 *   - открываете страницу товара с лотами других продавцов (так находятся цены рынка),
 * и дальше повторяет те же запросы для других товаров и цен.
 */
(function (root) {
  'use strict';

  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const leafKey = (path) => String(path).slice(String(path).lastIndexOf('.') + 1);
  const normKey = (k) => String(k).toLowerCase().replace(/[^a-z0-9]/g, '').replace(/^(is|has)(?=[a-z])/, '');
  const ok = (status) => status >= 200 && status < 300;
  const isMutating = (method) => !/^(GET|HEAD|OPTIONS)$/i.test(method || 'GET');
  const round2 = (x) => Math.round(x * 100) / 100;
  const floor2 = (x) => Math.floor(x * 100 + 1e-6) / 100;

  function parseJSON(text) {
    if (typeof text !== 'string' || !/^\s*[[{]/.test(text)) return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      return null;
    }
  }

  function first(d, ...keys) {
    if (!isObj(d)) return null;
    for (const k of keys) {
      const v = d[k];
      if (v !== undefined && v !== null && v !== '') return v;
    }
    return null;
  }

  function toPrice(v) {
    if (isObj(v)) v = first(v, 'amount', 'value', 'price');
    if (v === null || v === undefined || typeof v === 'boolean') return null;
    if (typeof v === 'number') return Number.isFinite(v) ? v : null;
    const s = String(v).replace(/[\s ]/g, '').replace(',', '.').replace(/^\$|\$$|₽$|€$/g, '');
    return /^-?\d+(\.\d+)?$/.test(s) ? parseFloat(s) : null;
  }

  /** Все скалярные значения объекта: [["product.id", "p1"], ["price", 12.5], ...]. */
  function flatten(obj, maxDepth = 5, limit = 800) {
    const out = [];
    (function walk(v, path, depth) {
      if (out.length >= limit) return;
      if (Array.isArray(v) || isObj(v)) {
        if (depth >= maxDepth) return;
        for (const k of Object.keys(v)) walk(v[k], path ? `${path}.${k}` : k, depth + 1);
      } else if (v !== null && v !== undefined) {
        out.push([path, v]);
      }
    })(obj, '', 0);
    return out;
  }

  function getPath(obj, path) {
    if (path === '' || path === null || path === undefined) return obj;
    let cur = obj;
    for (const k of String(path).split('.')) {
      if (cur === null || typeof cur !== 'object') return undefined;
      cur = cur[k];
    }
    return cur;
  }

  // ---------- цены, id и прочие поля ----------

  const NOT_OWN_PRICE_RE = /min|max|old|prev|orig|recommend|suggest|market|avg|average|median|lowest|highest|best|floor|fee|commission|total|start|initial|base|steam|rate|percent|discount|instant|buyout|limit/i;
  const MARKET_MIN_RE = /(min|lowest|best|floor)_?price|price_?(min|from|floor)|market_?price|^lowest$/i;

  /** Насколько похоже, что поле по этому пути — цена самого лота. */
  function priceScore(path) {
    const parts = String(path).split('.');
    const k = parts[parts.length - 1];
    const depth = parts.length - 1;
    const parent = parts.slice(0, -1).join('.');
    if (/^(amount|value)$/i.test(k) && /price$/i.test(parent) && !NOT_OWN_PRICE_RE.test(parent)) {
      return depth <= 1 ? 85 : 40;
    }
    if (!/price/i.test(k)) return depth === 0 && /^cost$/i.test(k) ? 30 : 0;
    if (NOT_OWN_PRICE_RE.test(k)) return 0;
    if (depth === 0) return /^price$/i.test(k) ? 100 : 90;
    if (depth === 1 && /^(sale|lot|offer|listing|order)s?$/i.test(parts[0])) return 80;
    if (depth <= 2 && !/seller|user|owner|stat|history/i.test(parent)) return 50;
    return 0;
  }

  /** Цена лота: {path, value} или null. */
  function listPrice(o) {
    if (!isObj(o)) return null;
    let best = null;
    for (const [path, v] of flatten(o, 3)) {
      const score = priceScore(path);
      if (!score) continue;
      let p = toPrice(v);
      if (p === null || p <= 0) continue;
      if (/cent|kopek/i.test(leafKey(path))) p = p / 100;
      if (!best || score > best.score) best = { path, value: p, score };
    }
    return best;
  }

  /** Сколько штук в записи (сайт может отдавать одинаковые товары одной строкой «×3»). */
  function qtyOf(o) {
    if (!isObj(o)) return 1;
    for (const k of Object.keys(o)) {
      if (!/^(quantity|qty|count|stack|stack_?size|items_?count|itemsCount)$/i.test(k)) continue;
      const n = toPrice(o[k]);
      if (Number.isInteger(n) && n >= 1 && n <= 100000) return n;
    }
    return 1;
  }

  /** Минимальная цена рынка, если сайт сам кладёт её в данные товара. */
  function marketMinField(o) {
    for (const [path, v] of flatten(o, 3)) {
      if (!MARKET_MIN_RE.test(leafKey(path))) continue;
      const p = toPrice(v);
      if (p !== null && p > 0) return p;
    }
    return null;
  }

  const ID_KEY_RE = /^(_?id|uuid|guid|uid)$|(_id|Id|ID|_uuid|Uuid|_uid)$/;
  const isIdKey = (k) => ID_KEY_RE.test(String(k));
  function strongId(v) {
    if (typeof v === 'string') return v.length >= 3 && v.length <= 200 && !/^(true|false|null|undefined)$/i.test(v);
    return typeof v === 'number' && Number.isInteger(v) && Math.abs(v) >= 100;
  }
  const USERISH_SEG_RE = /^(user|seller|owner|buyer|creator|author|customer|member|account|profile)s?(_?(id|uuid|name|nick|nickname|login))?$/i;
  const PRODUCTISH_SEG_RE = /^(product|catalog|template|category|game|kind|type|model|sku|class|collection|def|definition|pet|base_?item|item_?type|goods?|asset_?type)s?(_?(id|uuid|slug|name|key|code))?$/i;
  const isUserishPath = (p) => String(p).split('.').some((s) => USERISH_SEG_RE.test(s));
  const isProductishPath = (p) => String(p).split('.').some((s) => PRODUCTISH_SEG_RE.test(s));
  const inArray = (p) => /(^|\.)\d+(\.|$)/.test(p);

  const DONE_STATUS_RE = /sold|cancel|withdraw|complet|finish|expire|closed|delet|inactive|archiv|refund|bought|purchased/i;

  function statusOf(o) {
    for (const [path, v] of flatten(o, 2)) {
      if (/^(status|state|sale_?status|saleStatus)$/i.test(leafKey(path)) && typeof v === 'string') return v;
    }
    return '';
  }

  /** Лот ещё продаётся (не продан, не снят и т.п.). */
  const isActive = (o) => !DONE_STATUS_RE.test(statusOf(o));

  const NAME_KEY_RE = /^(name|title|display_?name|displayName|product_?name|productName|item_?name|itemName|pet_?name|petName)$/i;
  const NAME_SKIP_RE = /seller|user|owner|buyer|game|category|currency|status|rarity|account|creator|author|tag|collection/i;

  function itemName(o) {
    let best = null;
    for (const [path, v] of flatten(o, 4)) {
      if (typeof v !== 'string' || v.length < 2 || inArray(path)) continue;
      if (!NAME_KEY_RE.test(leafKey(path)) || NAME_SKIP_RE.test(path)) continue;
      const depth = path.split('.').length;
      if (!best || depth < best.depth) best = { v, depth };
    }
    return best ? best.v : '';
  }

  // ---------- вариант питомца (Neon / Mega, Fly, Ride) ----------

  const VARIANT_KEY_RE = /^(is_?|has_?)?(neon|mega|mega_?neon|meganeon|fly|flyable|ride|rideable|shiny|golden|rainbow)$/i;
  const VARIANT_CONTAINER_RE = /^(props|properties|attributes|attrs|variant|variants|flags|options|traits|modifiers)$/i;
  const VARIANT_WORDS = ['mega', 'neon', 'fly', 'ride'];

  function normVariant(k) {
    const s = String(k).toLowerCase().replace(/^(is|has)_?/, '').replace(/[^a-z]/g, '');
    return { flyable: 'fly', rideable: 'ride', meganeon: 'mega', m: 'mega', n: 'neon', f: 'fly', r: 'ride' }[s] || s;
  }

  function normBool(v) {
    if (v === true || v === 1 || /^(true|1|yes)$/i.test(String(v))) return 'true';
    if (v === false || v === 0 || /^(false|0|no)$/i.test(String(v))) return 'false';
    return String(v).trim().toLowerCase();
  }

  /** Признаки, по которым лоты одного питомца различаются: имя, neon/mega, fly/ride. */
  function variantSig(o) {
    const sig = new Map();
    const arrays = new Map();   // контейнер-список ["fly", "ride"] -> набор слов
    for (const [path, v] of flatten(o, 4)) {
      if (isUserishPath(path)) continue;
      const parts = path.split('.');
      const k = parts[parts.length - 1];
      const container = parts.slice(0, -1).find((s) => VARIANT_CONTAINER_RE.test(s));
      if (container && /^\d+$/.test(k)) {
        const key = parts.slice(0, -1).join('.');
        if (!arrays.has(key)) arrays.set(key, new Set());
        arrays.get(key).add(normVariant(v));
      } else if (VARIANT_KEY_RE.test(k) || (container && (typeof v === 'boolean' || /^(0|1|true|false)$/.test(String(v))))) {
        sig.set(normVariant(k), normBool(v));
      } else if (NAME_KEY_RE.test(k) && parts.length <= 2 && !NAME_SKIP_RE.test(path) && typeof v === 'string') {
        sig.set(`name@${path}`, v.trim().toLowerCase());
      }
    }
    for (const words of arrays.values()) {
      for (const w of VARIANT_WORDS) if (!sig.has(w)) sig.set(w, words.has(w) ? 'true' : 'false');
    }
    return sig;
  }

  /** Короткая подпись варианта, как принято в Adopt Me: M/N + F + R. */
  function variantLabel(o) {
    const sig = variantSig(o);
    const on = (w) => sig.get(w) === 'true';
    const s = (on('mega') ? 'M' : on('neon') ? 'N' : '') + (on('fly') ? 'F' : '') + (on('ride') ? 'R' : '');
    return s;
  }

  function currencyOf(list) {
    for (const o of list.slice(0, 20)) {
      for (const [path, v] of flatten(o, 3)) {
        if (/^currency(_?code)?$/i.test(leafKey(path)) && typeof v === 'string') {
          return { usd: '$', rub: '₽', eur: '€', uah: '₴', kzt: '₸' }[v.toLowerCase()] || v.toUpperCase();
        }
      }
    }
    return '';
  }

  // ---------- списки в ответах ----------

  function dictLists(body, path = '', depth = 0, out = []) {
    if (depth > 6 || out.length > 60) return out;
    if (Array.isArray(body)) {
      if (body.length && body.every(isObj)) out.push({ path, list: body });
    } else if (isObj(body)) {
      for (const k of Object.keys(body)) dictLists(body[k], path ? `${path}.${k}` : k, depth + 1, out);
    }
    return out;
  }

  const hasStrongId = (o) => flatten(o, 2).some(([p, v]) => isIdKey(leafKey(p)) && strongId(v));

  /** Списки лотов (у объектов есть цена; needId — и id) внутри ответа. */
  function priceLists(json, needId) {
    return dictLists(json).filter(({ list }) => {
      const n = Math.min(list.length, 50);
      let good = 0;
      for (let i = 0; i < n; i++) if (listPrice(list[i]) && (!needId || hasStrongId(list[i]))) good++;
      return good > 0 && good * 2 >= n;
    });
  }

  /** Список по пути из ответа (или самый большой список лотов, если путь не подошёл). */
  function listAt(json, listPath) {
    const v = getPath(json, listPath || '');
    if (Array.isArray(v)) return v;
    const lists = priceLists(json, false);
    if (!lists.length) return null;
    return lists.sort((a, b) => b.list.length - a.list.length)[0].list;
  }

  function identKey(o) {
    const v = first(o, 'id', '_id', 'sale_id', 'saleId', 'offer_id', 'offerId', 'lot_id', 'lotId', 'item_id', 'itemId', 'uuid');
    return v !== null ? `id:${v}` : JSON.stringify(o);
  }

  /** Пути id, которые у каждого объекта списка свои (id лота, id предмета...). */
  function uniqueIdPaths(list) {
    const counts = new Map();
    const sample = list.slice(0, 300);
    for (const o of sample) {
      for (const [p, v] of flatten(o, 4)) {
        if (inArray(p) || !isIdKey(leafKey(p)) || !strongId(v)) continue;
        if (!counts.has(p)) counts.set(p, new Map());
        const m = counts.get(p);
        m.set(String(v), (m.get(String(v)) || 0) + 1);
      }
    }
    const out = [];
    for (const [p, m] of counts) {
      let total = 0;
      for (const c of m.values()) total += c;
      if (total >= Math.max(1, Math.ceil(sample.length * 0.8)) && m.size === total) out.push(p);
    }
    const depth = (p) => p.split('.').length;
    return out.sort((a, b) => depth(a) - depth(b) || (leafKey(a) === 'id' ? -1 : 0) - (leafKey(b) === 'id' ? -1 : 0));
  }

  /** Всё, что нужно знать о списке ваших товаров. */
  function describeMine(list, extra = {}) {
    const active = list.filter((o) => isActive(o) && listPrice(o));
    const unique = uniqueIdPaths(active.length ? active : list);
    let ownPaths = unique.filter((p) => !isUserishPath(p) && !isProductishPath(p));
    if (extra.refPaths) ownPaths = [...new Set([...extra.refPaths, ...ownPaths])];
    if (!ownPaths.length) ownPaths = unique.filter((p) => !isUserishPath(p));
    const ownValues = new Set();
    for (const o of list) {
      for (const p of ownPaths) {
        const v = getPath(o, p);
        if (v !== undefined && v !== null) ownValues.add(String(v));
      }
    }
    // ваш id: поле продавца/владельца с одинаковым значением у всех ваших лотов
    let uid = null;
    if (active.length) {
      for (const [p, v] of flatten(active[0], 3)) {
        if (!isUserishPath(p) || !(isIdKey(leafKey(p)) || USERISH_SEG_RE.test(leafKey(p))) || !strongId(v)) continue;
        if (active.every((o) => String(getPath(o, p)) === String(v))) {
          uid = String(v);
          break;
        }
      }
    }
    return {
      list: active,
      skipped: list.length - active.length,
      idPaths: unique,
      ownPaths,
      ownValues,
      uid,
      keyPath: ownPaths[0] || null,
      currency: currencyOf(list),
    };
  }

  function itemKey(o, mine) {
    if (mine && mine.keyPath) {
      const v = getPath(o, mine.keyPath);
      if (v !== undefined && v !== null) return String(v);
    }
    return identKey(o);
  }

  /** Этот лот — ваш (по id лота/предмета или по id продавца). */
  function isMine(x, mine) {
    for (const [p, v] of flatten(x, 3)) {
      if (isIdKey(leafKey(p)) && !isProductishPath(p) && !isUserishPath(p) && mine.ownValues.has(String(v))) return true;
      if (mine.uid !== null && mine.uid !== undefined && isUserishPath(p) && String(v) === mine.uid) return true;
    }
    return false;
  }

  // ---------- запросы сайта: значения и шаблоны ----------

  const SLUG = {
    kebab: (s) => String(s).trim().toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9а-яё]+/g, '-').replace(/^-+|-+$/g, ''),
    snake: (s) => String(s).trim().toLowerCase().replace(/['’]/g, '').replace(/[^a-z0-9а-яё]+/g, '_').replace(/^_+|_+$/g, ''),
    lower: (s) => String(s).toLowerCase(),
    b01: (v) => (normBool(v) === 'true' ? '1' : normBool(v) === 'false' ? '0' : String(v)),
    bool: (v) => normBool(v),
  };

  function safeDecode(s) {
    try {
      return decodeURIComponent(String(s).replace(/\+/g, ' '));
    } catch (e) {
      return String(s);
    }
  }

  function splitQuery(search) {
    const s = String(search || '').replace(/^\?/, '');
    if (!s) return [];
    return s.split('&').filter(Boolean).map((part) => {
      const i = part.indexOf('=');
      return i < 0 ? [safeDecode(part), ''] : [safeDecode(part.slice(0, i)), safeDecode(part.slice(i + 1))];
    });
  }

  function bodyPairs(cap) {
    if (cap.bodyKind === 'form') return splitQuery(cap.body);
    if (cap.bodyKind === 'formdata') {
      const v = parseJSON(cap.body);
      return Array.isArray(v) ? v.map(([k, x]) => [String(k), String(x)]) : [];
    }
    return null;
  }

  /** Все значения, которые запрос передаёт сайту (части адреса, параметры, тело). */
  function refValues(cap) {
    const out = new Set();
    let u;
    try {
      u = new URL(cap.url);
    } catch (e) {
      return out;
    }
    for (const seg of u.pathname.split('/')) if (seg) out.add(safeDecode(seg));
    for (const [, v] of splitQuery(u.search)) out.add(v);
    const pairs = bodyPairs(cap);
    if (pairs) for (const [, v] of pairs) out.add(v);
    if (cap.bodyKind === 'json') for (const [, v] of flatten(parseJSON(cap.body), 8, 3000)) out.add(String(v));
    if (cap.bodyKind === 'text' && cap.body) for (const m of String(cap.body).match(/[A-Za-z0-9_-]{3,}/g) || []) out.add(m);
    return out;
  }

  const PRICE_SLOT_RE = /price/i;
  const NOT_SLOT_RE = /old|prev|orig|current|was|before|min|max|recommend|suggest|market|currency|count|qty|quantity|percent/i;

  function slotKind(cap) {
    // есть ли в запросе поле с «price» в имени; если нет — годятся cost/amount/sum
    const keys = [];
    let u;
    try {
      u = new URL(cap.url);
    } catch (e) {
      return 'price';
    }
    for (const [k] of splitQuery(u.search)) keys.push(k);
    const pairs = bodyPairs(cap);
    if (pairs) for (const [k] of pairs) keys.push(k);
    if (cap.bodyKind === 'json') for (const [p] of flatten(parseJSON(cap.body), 8, 3000)) keys.push(...p.split('.'));
    return keys.some((k) => PRICE_SLOT_RE.test(k) && !NOT_SLOT_RE.test(k)) ? 'price' : 'amount';
  }

  function isSlotKey(k, kind) {
    if (!k || NOT_SLOT_RE.test(k)) return false;
    return kind === 'price' ? PRICE_SLOT_RE.test(k) : /^(cost|amount|sum)$/i.test(k);
  }

  function valueIndex(o, exclude) {
    const byValue = new Map();
    const byKey = new Map();
    const depth = (p) => p.split('.').length;
    const add = (s, hit) => {
      const cur = byValue.get(s);
      if (!cur || depth(hit.path) < depth(cur.path)) byValue.set(s, hit);
    };
    for (const [path, v] of flatten(o, 5)) {
      if (exclude && exclude(path)) continue;
      const nk = normKey(leafKey(path));
      if (!byKey.has(nk)) byKey.set(nk, []);
      byKey.get(nk).push({ path, v });
      if (priceScore(path) || MARKET_MIN_RE.test(leafKey(path)) || typeof v === 'boolean') continue;
      const s = String(v);
      if (typeof v === 'number' ? !(Number.isInteger(v) && Math.abs(v) >= 100) : s.length < 3 || /^(true|false|null)$/i.test(s)) continue;
      add(s, { path, fn: '' });
      if (typeof v === 'string') {
        for (const fn of ['kebab', 'snake', 'lower']) {
          const sv = SLUG[fn](v);
          if (sv.length >= 3 && sv !== s) add(sv, { path, fn });
        }
      }
    }
    return { byValue, byKey };
  }

  function matchValue(idx, s, key) {
    const hit = idx.byValue.get(String(s));
    if (hit) return hit;
    if (key) {
      // ?fly=true, ?neon=1, ?name=frost%20dragon — по имени поля
      const flag = /^(0|1|true|false)$/i.test(String(s));
      for (const { path, v } of idx.byKey.get(normKey(key)) || []) {
        if (String(v) === String(s)) return { path, fn: '' };
        if (typeof v === 'string' && v.toLowerCase() === String(s).toLowerCase()) return { path, fn: 'lower' };
        // признак варианта (fly, neon...) — подставляем значение товара, даже если у образца было другое
        if (flag && (typeof v === 'boolean' || v === 0 || v === 1)) return { path, fn: /^[01]$/.test(String(s)) ? 'b01' : 'bool' };
      }
    }
    return null;
  }

  const decimalsOf = (v) => {
    const m = String(v).match(/[.,](\d+)$/);
    return m ? m[1].length : 0;
  };

  /**
   * Шаблон запроса: значения из объекта o -> {{o:путь}}, цена -> {{price:N}}.
   * opts.withPrice — искать поля цены; opts.exclude(path) — какие поля объекта не подставлять.
   */
  function makeTemplate(cap, o, opts = {}) {
    const idx = valueIndex(o, opts.exclude);
    const vars = [];
    const slots = [];
    const kind = opts.withPrice ? slotKind(cap) : null;
    const token = (hit) => {
      vars.push(hit.path);
      return `{{o:${hit.path}${hit.fn ? '|' + hit.fn : ''}}}`;
    };
    const slot = (key, raw) => {
      slots.push({ key, raw, type: typeof raw === 'string' ? 'str' : 'num', decimals: decimalsOf(raw), int: Number.isInteger(toPrice(raw)) });
      return slots.length - 1;
    };
    const pairTpl = (pairs) => pairs.map(([k, v]) => {
      if (kind && isSlotKey(k, kind) && toPrice(v) !== null) return [k, `{{price:${slot(k, v)}}}`];
      const hit = matchValue(idx, v, k);
      return [k, hit ? token(hit) : v];
    });

    const u = new URL(cap.url);
    const segs = u.pathname.split('/');
    const path = segs.map((seg, i) => {
      if (!seg) return seg;
      const dec = safeDecode(seg);
      if (kind && i > 0 && isSlotKey(safeDecode(segs[i - 1]), kind) && toPrice(dec) !== null) return `{{price:${slot(segs[i - 1], dec)}}}`;
      const hit = matchValue(idx, dec, null);
      return hit ? token(hit) : seg;
    });
    const query = pairTpl(splitQuery(u.search));

    let body = null;
    if (cap.bodyKind === 'json') {
      const walk = (v, key) => {
        if (Array.isArray(v)) return v.map((x) => walk(x, key));
        if (isObj(v)) {
          const out = {};
          for (const k of Object.keys(v)) {
            const x = v[k];
            if (kind && isSlotKey(k, kind) && isObj(x) && toPrice(first(x, 'amount', 'value')) !== null) {
              const inner = 'amount' in x ? 'amount' : 'value';
              out[k] = { ...walk(x, k), [inner]: { $spx: 'price', i: slot(k, x[inner]) } };
            } else {
              out[k] = walk(x, k);
            }
          }
          return out;
        }
        if (v === null || v === undefined) return v;
        if (kind && key && isSlotKey(key, kind) && toPrice(v) !== null) return { $spx: 'price', i: slot(key, v) };
        if (typeof v === 'boolean' && !(key && (idx.byKey.get(normKey(key)) || []).length)) return v;
        const hit = matchValue(idx, String(v), key);
        if (!hit) return v;
        vars.push(hit.path);
        return { $spx: 'o', path: hit.path, fn: hit.fn, t: typeof v };
      };
      body = walk(parseJSON(cap.body), null);
    } else if (cap.bodyKind === 'form' || cap.bodyKind === 'formdata') {
      body = pairTpl(bodyPairs(cap));
    } else if (cap.bodyKind === 'text' && cap.body) {
      body = String(cap.body);
      for (const [s, hit] of [...idx.byValue].sort((a, b) => b[0].length - a[0].length)) {
        if (s.length >= 6 && body.includes(s)) body = body.split(s).join(token(hit));
      }
    }

    // цена в запросе: главное поле — «price», остальные — в той же пропорции (цена с комиссией и т.п.)
    const mainIdx = Math.max(0, slots.findIndex((s) => normKey(s.key) === 'price'));
    const mainRaw = slots.length ? toPrice(slots[mainIdx].raw) : null;
    const prices = slots.map((s) => ({
      key: s.key,
      type: s.type,
      decimals: s.decimals,
      int: s.int,
      ratio: mainRaw ? toPrice(s.raw) / mainRaw : 1,
    }));

    return {
      method: String(cap.method || 'GET').toUpperCase(),
      origin: u.origin,
      path,
      query,
      bodyKind: body === null ? 'none' : cap.bodyKind,
      body,
      contentType: cap.contentType || '',
      prices,
      reqPrice: mainRaw,
      vars: [...new Set(vars)],
    };
  }

  function priceOut(slot, price, scale) {
    let v = price * (scale || 1) * (slot.ratio || 1);
    const asInt = slot.int && (scale || 1) !== 1;
    v = asInt ? Math.round(v) : round2(v);
    if (slot.type === 'str') return asInt ? String(v) : v.toFixed(2);
    return v;
  }

  /**
   * Готовый запрос из шаблона для объекта o и цены price.
   * lenient — параметр адреса, для которого у товара нет поля, просто не передаётся.
   */
  function fillTemplate(tpl, o, price, scale = 1, lenient = false) {
    const oval = (path, fn) => {
      const v = getPath(o, path);
      if (v === undefined || v === null) throw new Error(`у товара нет поля «${path}»`);
      return fn ? SLUG[fn](v) : v;
    };
    const pval = (i) => {
      if (price === null || price === undefined) throw new Error('не задана цена');
      return priceOut(tpl.prices[i], price, scale);
    };
    const fillStr = (s) => String(s).replace(/\{\{(o|price):([^}|]+)(?:\|(\w+))?\}\}/g,
      (_, kind, a, fn) => String(kind === 'o' ? oval(a, fn) : pval(+a)));
    const enc = encodeURIComponent;

    let url = tpl.origin + tpl.path.map((s) => (s.includes('{{') ? enc(fillStr(s)) : s)).join('/');
    const query = [];
    for (const [k, v] of tpl.query) {
      try {
        query.push(`${enc(k)}=${enc(fillStr(v))}`);
      } catch (e) {
        if (!lenient || /\{\{price:/.test(v)) throw e;
      }
    }
    if (query.length) url += '?' + query.join('&');

    const fillJSON = (v) => {
      if (Array.isArray(v)) return v.map(fillJSON);
      if (isObj(v)) {
        if (v.$spx === 'price') return pval(v.i);
        if (v.$spx === 'o') {
          const x = oval(v.path, v.fn);
          if (v.t === 'boolean') return normBool(x) === 'true';
          if (v.t === 'number' && toPrice(x) !== null) return Number(x);
          if (v.t === 'string') return String(x);
          return x;
        }
        const out = {};
        for (const k of Object.keys(v)) out[k] = fillJSON(v[k]);
        return out;
      }
      return v;
    };

    let body = null;
    if (tpl.bodyKind === 'json') body = JSON.stringify(fillJSON(tpl.body));
    else if (tpl.bodyKind === 'form') body = tpl.body.map(([k, v]) => `${enc(k)}=${enc(fillStr(v))}`).join('&');
    else if (tpl.bodyKind === 'formdata') body = JSON.stringify(tpl.body.map(([k, v]) => [k, fillStr(v)]));
    else if (tpl.bodyKind === 'text') body = fillStr(tpl.body);
    return { method: tpl.method, url, bodyKind: tpl.bodyKind, body, contentType: tpl.contentType };
  }

  /** Короткая запись шаблона для показа: PATCH /api/v2/sales/{id}. */
  function describeTemplate(tpl) {
    const show = (s) => String(s)
      .replace(/\{\{o:([^}|]+)(?:\|\w+)?\}\}/g, (_, p) => `{${leafKey(p)}}`)
      .replace(/\{\{price:\d+\}\}/g, '{цена}');
    let s = `${tpl.method} ${show(tpl.path.join('/'))}`;
    if (tpl.query.length) s += '?' + tpl.query.map(([k, v]) => `${k}=${show(v)}`).join('&');
    return s;
  }

  // ---------- обучение ----------

  const USER_URL_RE = /\/(me|my|mine|self|user|users|profile|account|inventory|seller|personal|cabinet|dashboard)(\/|$)|my[-_]?(items|sales|lots|offers|inventory|listings|products)|(user|my)[-_]?(items|sales|lots|offers|listings|inventory)/i;
  const NOT_MINE_URL_RE = /history|transaction|purchase|deposit|withdraw|payment|notification|statistic|review|cart|favou?rite|wishlist|\/orders?(\/|$)|\/logs?(\/|$)/i;
  // запросы, которые точно не смена цены своего лота
  const NOT_EDIT_URL_RE = /cart|basket|buy|purchase|checkout|\/orders?(\/|$)|withdraw|deposit|trade|favou?rite|wish|like|report|review|chat|message|support|ticket|promo|bonus|login|logout|auth|register|refresh/i;

  /** Похоже ли, что этот ответ — список ваших товаров на продаже. */
  function guessMine(cap) {
    if (cap.method !== 'GET' || !ok(cap.status) || !cap.json) return null;
    let u;
    try {
      u = new URL(cap.url);
    } catch (e) {
      return null;
    }
    if (!USER_URL_RE.test(u.pathname) || NOT_MINE_URL_RE.test(u.pathname)) return null;
    const lists = priceLists(cap.json, true);
    if (!lists.length) return null;
    const best = lists.sort((a, b) => b.list.length - a.list.length)[0];
    return { op: { method: 'GET', url: cap.url, bodyKind: 'none', body: null, contentType: '', listPath: best.path }, list: best.list };
  }

  /** Запрос только читает данные: GET, запрос GraphQL (не mutation) или POST-поиск без цены. */
  function isReadLike(cap) {
    if (!isMutating(cap.method)) return true;
    if (String(cap.method).toUpperCase() !== 'POST') return false;
    const body = cap.bodyKind === 'json' ? parseJSON(cap.body) : null;
    if (isObj(body) && typeof body.query === 'string') return !/^\s*mutation\b/.test(body.query);
    return /(^|\/)(graphql|search|list|filter|find|query)(\/|$)/i.test(urlPath(cap.url))
      && !makeTemplate(cap, {}, { withPrice: true }).prices.length;
  }

  const cmpScore = (a, b) => {
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
    return 0;
  };

  function urlPath(url) {
    try {
      return new URL(url).pathname;
    } catch (e) {
      return '';
    }
  }

  /** Список может быть вашим: адрес «пользовательский» или у всех лотов один продавец. */
  function mineLike(entry, mineUrl) {
    if (mineUrl && sameEndpoint(entry.cap.url, mineUrl)) return true;
    const path = urlPath(entry.cap.url);
    if (USER_URL_RE.test(path) && !NOT_MINE_URL_RE.test(path)) return true;
    return entry.list.length >= 2 && describeMine(entry.list).uid !== null;
  }

  /**
   * Смена цены: изменяющий запрос сайта с id одного вашего лота и полем цены.
   * lists — списки лотов, которые сайт получал раньше ({cap, path, list}),
   * muts — недавние изменяющие запросы (для «снять и выставить заново»),
   * mineUrl — адрес списка ваших товаров, если он уже известен.
   */
  function learnEdit(cap, lists, muts, mineUrl) {
    if (isReadLike(cap) || !ok(cap.status) || NOT_EDIT_URL_RE.test(urlPath(cap.url))) return null;
    const probe = makeTemplate(cap, {}, { withPrice: true });
    if (!probe.prices.length) return null;
    const refs = refValues(cap);
    let best = null;
    for (const entry of lists) {
      if (!isReadLike(entry.cap)) continue;
      const ids = uniqueIdPaths(entry.list).filter((p) => !isUserishPath(p));
      const hit = [];
      for (const o of entry.list) {
        const paths = ids.filter((p) => {
          const v = getPath(o, p);
          return v !== undefined && v !== null && refs.has(String(v));
        });
        if (paths.length) hit.push({ o, paths });
      }
      if (!hit.length || !mineLike(entry, mineUrl)) continue;
      if (hit.length > 1 && hit.some((h) => h.paths.some((p) => !isProductishPath(p)))) return { error: 'batch' };
      const score = [
        mineUrl && sameEndpoint(entry.cap.url, mineUrl) ? 1 : 0,
        USER_URL_RE.test(urlPath(entry.cap.url)) ? 1 : 0,
        (entry.cap.method || 'GET') === 'GET' ? 1 : 0,
        entry.cap.seq || 0,
        entry.list.length,
      ];
      if (!best || cmpScore(score, best.score) > 0) best = { entry, o: hit[0].o, paths: hit[0].paths, score };
    }
    if (!best) return null;
    const { entry, o, paths } = best;
    const refPaths = paths.filter((p) => !isProductishPath(p));
    if (!refPaths.length) return null;
    const exclude = (p) => priceScore(p) > 0;
    const main = makeTemplate(cap, o, { withPrice: true, exclude });
    if (!main.vars.some((v) => refPaths.includes(v))) return null;
    // перед этим сайт мог снять лот (смена цены = снять и выставить заново)
    const ownIds = uniqueIdPaths(entry.list).filter((p) => !isUserishPath(p) && !isProductishPath(p));
    const ids = new Set([...refPaths, ...ownIds].map((p) => getPath(o, p)).filter((v) => v !== undefined && v !== null).map(String));
    const pre = (muts || []).filter((m) => m !== cap && (m.seq || 0) < (cap.seq || 0) && ok(m.status) && !isReadLike(m)
      && (cap.t || 0) - (m.t || 0) < 30000 && !NOT_EDIT_URL_RE.test(urlPath(m.url))
      && !makeTemplate(m, {}, { withPrice: true }).prices.length
      && [...refValues(m)].some((v) => ids.has(v)));
    const steps = [];
    if (pre.length) steps.push(makeTemplate(pre[pre.length - 1], o, { exclude }));
    steps.push(main);
    return {
      edit: { steps, refPaths: [...new Set([...refPaths, ...ownIds])] },
      mine: {
        method: entry.cap.method || 'GET',
        url: entry.cap.url,
        bodyKind: entry.cap.bodyKind || 'none',
        body: entry.cap.body || null,
        contentType: entry.cap.contentType || '',
        listPath: entry.path,
      },
      sample: o,
      ids: [...ids],
      reqPrice: main.reqPrice,
    };
  }

  /**
   * Лоты товара на рынке: GET (или поиск POST без цены), в адресе — данные одного
   * из ваших товаров (id товара, имя...), в ответе — список лотов с ценами.
   */
  function learnLots(cap, mine, mineUrl) {
    if (!ok(cap.status) || !cap.json || !isReadLike(cap)) return null;
    if (mineUrl && sameEndpoint(cap.url, mineUrl)) return null;
    const lists = priceLists(cap.json, false);
    if (!lists.length) return null;
    const L = lists.sort((a, b) => b.list.length - a.list.length)[0];
    // это список ваших же товаров, а не рынок: других продавцов в нём нет
    if (L.list.every((x) => isMine(x, mine))) return null;
    const own = new Set(mine.ownPaths);
    const exclude = (p) => own.has(p) || isUserishPath(p) || priceScore(p) > 0;
    const distinctive = (p) => isProductishPath(p) || NAME_KEY_RE.test(leafKey(p)) || /slug|sku|code|key/i.test(leafKey(p))
      || (isIdKey(leafKey(p)) && !own.has(p));
    let best = null;
    for (const o of mine.list.slice(0, 300)) {
      const tpl = makeTemplate(cap, o, { exclude });
      const strong = tpl.vars.filter(distinctive);
      if (!strong.length) continue;
      const score = strong.length * 10 + tpl.vars.length;
      if (!best || score > best.score) best = { o, tpl, score };
    }
    if (!best) return null;
    return { lots: { ...best.tpl, listPath: L.path }, sample: best.o };
  }

  function sameEndpoint(a, b) {
    try {
      const ua = new URL(a);
      const ub = new URL(b);
      return ua.origin === ub.origin && ua.pathname.replace(/\/$/, '') === ub.pathname.replace(/\/$/, '');
    } catch (e) {
      return false;
    }
  }

  /** Тот же способ смены цены (те же запросы), только с другими значениями. */
  function sameEdit(a, b) {
    if (!a || !b || a.steps.length !== b.steps.length) return false;
    return a.steps.every((s, i) => s.origin === b.steps[i].origin && describeTemplate(s) === describeTemplate(b.steps[i]));
  }

  /** Тот же товар и вариант (имя, neon/mega, fly/ride, id товара из шаблона). */
  function sameProduct(x, o, tpl) {
    const so = variantSig(o);
    const sx = variantSig(x);
    for (const [k, v] of so) if (sx.has(k) && sx.get(k) !== v) return false;
    for (const p of (tpl && tpl.vars) || []) {
      const a = getPath(x, p);
      const b = getPath(o, p);
      if (a !== undefined && a !== null && b !== undefined && b !== null && typeof a !== 'object' && String(a) !== String(b)) return false;
    }
    return true;
  }

  /** Минимальная цена других продавцов из ответа с лотами товара. */
  function minFromLots(json, tpl, o, mine) {
    const list = listAt(json, tpl.listPath) || [];
    let mineCount = 0;
    let otherVariant = 0;
    const prices = [];
    for (const x of list) {
      if (!isObj(x)) continue;
      if (isMine(x, mine)) {
        mineCount++;
        continue;
      }
      if (!isActive(x)) continue;
      if (!sameProduct(x, o, tpl)) {
        otherVariant++;
        continue;
      }
      const p = listPrice(x);
      if (p) prices.push(p.value);
    }
    return { min: prices.length ? Math.min(...prices) : null, count: prices.length, mineCount, otherVariant };
  }

  /** Во сколько раз цена в запросе сайта больше цены в списке (центы -> 100). */
  function detectScale(reqPrice, listPriceValue) {
    if (!reqPrice || !listPriceValue) return null;
    const r = reqPrice / listPriceValue;
    for (const s of [1, 100, 1000, 0.01]) if (Math.abs(r / s - 1) < 0.02) return s;
    return null;
  }

  // ---------- постраничная загрузка ----------

  const PAGE_KEYS = ['page', 'p', 'pageNumber', 'page_number', 'pageNum', 'current_page', 'currentPage', 'pageIndex', 'page_index'];
  const OFFSET_KEYS = ['offset', 'skip', 'start', 'from'];
  const LIMIT_KEYS = ['limit', 'per_page', 'perPage', 'page_size', 'pageSize', 'size', 'take', 'count'];
  const TOTAL_KEYS = ['total', 'total_count', 'totalCount', 'total_items', 'totalItems', 'count'];
  const MORE_KEYS = ['has_more', 'hasMore', 'has_next', 'hasNext', 'hasNextPage', 'has_next_page'];
  const PAGES_KEYS = ['pages', 'total_pages', 'totalPages', 'last_page', 'lastPage', 'page_count', 'pageCount'];
  const NEXT_KEYS = ['next', 'next_page', 'nextPage', 'next_url', 'nextUrl', 'next_page_url'];
  const CURSOR_KEYS = ['next_cursor', 'nextCursor', 'cursor', 'after', 'next_token', 'nextToken'];

  function pageHints(body, n) {
    const hints = { more: null, total: null, pages: null, next: null, cursor: null };
    if (!isObj(body)) return hints;
    const blocks = [body];
    for (const k of ['meta', 'pagination', 'paging', 'page_info', 'pageInfo', 'data']) if (isObj(body[k])) blocks.push(body[k]);
    for (const b of blocks) {
      for (const k of TOTAL_KEYS) if (Number.isInteger(b[k]) && b[k] >= n) hints.total = b[k];
      for (const k of MORE_KEYS) if (typeof b[k] === 'boolean') hints.more = b[k] || !!hints.more;
      for (const k of PAGES_KEYS) if (Number.isInteger(b[k]) && b[k] >= 1) hints.pages = b[k];
      for (const k of NEXT_KEYS) {
        const v = b[k];
        if (typeof v === 'string' && v && (v.startsWith('/') || v.includes('://') || v.includes('?'))) hints.next = v;
      }
      for (const k of CURSOR_KEYS) {
        const v = b[k];
        if ((typeof v === 'string' && v) || Number.isInteger(v)) hints.cursor = [k, v];
      }
    }
    return hints;
  }

  /** Весь список (все страницы) по запросу op; send(req) -> {status, json, text, error}. */
  async function fetchAllPages(op, send, maxPages = 60) {
    const u = new URL(op.url);
    const q = u.searchParams;
    let pageKey = PAGE_KEYS.find((k) => q.has(k)) || null;
    const offKey = pageKey ? null : OFFSET_KEYS.find((k) => q.has(k)) || null;
    const limitKey = LIMIT_KEYS.find((k) => q.has(k)) || null;
    const zeroBased = !!pageKey && q.get(pageKey) === '0';
    let page = pageKey ? (zeroBased ? 0 : 1) : null;
    let offset = 0;
    let knownTotal = null;
    let cursor = null;
    let nextUrl = null;
    const out = [];
    const seen = new Set();
    for (let i = 0; i < maxPages; i++) {
      if (pageKey) q.set(pageKey, String(page));
      if (offKey) q.set(offKey, String(offset));
      if (cursor) q.set(cursor[0] === 'next_cursor' || cursor[0] === 'nextCursor' ? 'cursor' : cursor[0], String(cursor[1]));
      const url = nextUrl || u.href;
      const res = await send({ method: op.method || 'GET', url, bodyKind: op.bodyKind || 'none', body: op.body || null, contentType: op.contentType || '' });
      if (!ok(res.status)) {
        if (i === 0) return { list: null, status: res.status, error: res.error || `сайт ответил ${res.status}`, text: res.text };
        break;
      }
      const list = listAt(res.json, op.listPath);
      if (!list) {
        if (i === 0) return { list: null, status: res.status, error: 'в ответе сайта нет списка товаров', text: res.text };
        break;
      }
      let added = 0;
      for (const o of list) {
        const id = identKey(o);
        if (!seen.has(id)) {
          seen.add(id);
          out.push(o);
          added++;
        }
      }
      if (!list.length || !added) break;
      const hints = pageHints(res.json, list.length);
      if (hints.total !== null && hints.total > list.length) knownTotal = Math.max(knownTotal || 0, hints.total);
      if (knownTotal !== null && out.length >= knownTotal) break;
      if (limitKey && Number(q.get(limitKey)) > 0 && list.length < Number(q.get(limitKey))) break;
      if (pageKey) {
        if (hints.pages !== null && page >= hints.pages - (zeroBased ? 1 : 0) && hints.more !== true) break;
        if (hints.more === false) break;
        page++;
      } else if (offKey) {
        if (hints.more === false) break;
        offset += list.length;
      } else if (hints.next) {
        nextUrl = new URL(hints.next, u.href).href;
      } else if (hints.cursor && hints.more !== false) {
        cursor = hints.cursor;
      } else if (hints.more || (hints.pages !== null && hints.pages > 1) || (knownTotal !== null && out.length < knownTotal)) {
        pageKey = 'page';
        page = 2;
      } else {
        break;
      }
    }
    return { list: out, status: 200 };
  }

  // ---------- расчёт новой цены ----------

  /**
   * Что сделать с товаром: change / same / skip.
   * s: {undercut, floor, maxDrop, raise}
   */
  function planPrice(current, marketMin, s) {
    if (marketMin === null || marketMin === undefined) return { action: 'skip', reason: 'на рынке нет других продавцов' };
    let target = floor2(marketMin - (Number(s.undercut) || 0));
    let note = '';
    if (Number(s.floor) > 0 && target < Number(s.floor)) {
      target = round2(Number(s.floor));
      note = `не ниже ${target.toFixed(2)}`;
    }
    if (target <= 0) return { action: 'skip', reason: 'новая цена получилась нулевой', target };
    if (Math.abs(target - current) < 0.005) return { action: 'same', reason: 'уже стоит по минимальной цене', target };
    if (target > current && !s.raise) {
      return { action: 'same', reason: `вы и так дешевле всех (у других от ${marketMin.toFixed(2)})`, target: current };
    }
    const drop = current > 0 ? ((current - target) / current) * 100 : 0;
    return { action: 'change', target, drop, note, big: drop > (Number(s.maxDrop) || 100) };
  }

  // ---------- отчёт для разработчика ----------

  const JWT_RE = /eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*/g;
  const BEARER_RE = /(Bearer\s+)[^\s"',]+/gi;
  const TOKEN_FIELD_RE = /("[A-Za-z_]*(?:token|secret|password|session|cookie|csrf)[A-Za-z_]*"\s*:\s*)"[^"]*"/gi;
  const TOKEN_PARAM_RE = /([?&][^=&]*(?:token|secret|password|session|key|sign)[^=&]*=)[^&#]*/gi;
  const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

  function redact(text) {
    return String(text)
      .replace(JWT_RE, '<token>')
      .replace(BEARER_RE, '$1<token>')
      .replace(TOKEN_FIELD_RE, '$1"<token>"')
      .replace(TOKEN_PARAM_RE, '$1<token>')
      .replace(EMAIL_RE, '<email>');
  }

  const api = {
    VERSION: '1.0.0',
    isObj, ok, isMutating, parseJSON, first, toPrice, flatten, getPath, leafKey, round2, floor2,
    priceScore, listPrice, qtyOf, marketMinField, isReadLike, sameEdit, isIdKey, statusOf, isActive, itemName, variantSig, variantLabel,
    currencyOf, dictLists, priceLists, listAt, identKey, uniqueIdPaths, describeMine, itemKey, isMine,
    refValues, makeTemplate, fillTemplate, describeTemplate, guessMine, learnEdit, learnLots, sameEndpoint,
    sameProduct, minFromLots, detectScale, pageHints, fetchAllPages, planPrice, redact,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SPX = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
