/*
 * Проверка расширения в настоящем Chromium на имитации starpets.gg (mock_site.js).
 * Запуск: node tests/starpets/e2e.js   (нужен пакет playwright и его Chromium;
 * на Linux без экрана — через xvfb-run).
 *
 * «Пользователь» делает то, что просит расширение: открывает свои товары,
 * один раз меняет цену вручную и открывает страницу товара. Потом расширение
 * само ставит выбранные товары на минимальную цену рынка.
 */
'use strict';

const path = require('path');
const fs = require('fs');
const os = require('os');
const assert = require('assert/strict');
const { chromium } = require('playwright');
const mock = require('./mock_site.js');

const EXT = path.resolve(__dirname, '../../starpets_extension');
const SHOTS = process.env.SPX_SHOTS || '';
const P = '#starpets-helper';

const num = (t) => parseFloat(String(t).replace(/[^\d,.-]/g, '').replace(',', '.'));
const floor2 = (x) => Math.floor(x * 100 + 1e-6) / 100;

async function waitFor(what, fn, timeout = 20000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) {
      last = e;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`не дождался: ${what} (последнее: ${last instanceof Error ? last.message : JSON.stringify(last)})`);
}

const text = (page, sel) => page.locator(`${P} ${sel}`).first().innerText();

/** Чего ждём от расширения: минимальная цена других продавцов того же варианта. */
function expected(state) {
  const plans = new Map();
  for (const l of state.lots.filter((x) => x.mine && x.status === 'active')) {
    const others = state.lots.filter((x) => !x.mine && x.status === 'active' && x.name === l.name && x.v === l.v);
    const min = others.length ? Math.min(...others.map((x) => x.price)) : null;
    let action = 'skip';
    let target = null;
    if (min !== null) {
      target = floor2(min);
      action = Math.abs(target - l.price) < 0.005 || target > l.price ? 'same' : 'change';
    }
    plans.set(l.n, { action, target, big: action === 'change' && (l.price - target) / l.price > 0.3, price: l.price });
  }
  return plans;
}

async function shot(page, name) {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}

async function launch(port) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'spx-e2e-'));
  const ctx = await chromium.launchPersistentContext(dir, {
    headless: false,
    viewport: { width: 1280, height: 860 },
    args: [
      `--disable-extensions-except=${EXT}`,
      `--load-extension=${EXT}`,
      `--host-resolver-rules=MAP starpets.gg:80 127.0.0.1:${port},MAP api.starpets.gg:80 127.0.0.1:${port}`,
      '--no-proxy-server',
    ],
  });
  const errors = [];
  ctx.on('page', (p) => p.on('pageerror', (e) => errors.push(String(e))));
  return { ctx, errors, dir };
}

async function totals(page) {
  return { total: num(await text(page, '[data-f=total]')), net: num(await text(page, '[data-f=net]')) };
}

async function modalRows(page) {
  await page.locator(`${P} .modal`).waitFor();
  return {
    title: await page.locator(`${P} .modal h3`).innerText(),
    body: await page.locator(`${P} .modal .mbody`).innerText(),
  };
}

async function closeModal(page, label = 'OK') {
  await page.locator(`${P} .modal .mfoot button`, { hasText: label }).click();
  await page.locator(`${P} .modal`).waitFor({ state: 'detached' });
}

async function scenario(variant) {
  console.log(`\n=== вариант ${variant} ===`);
  const site = await mock.start(variant);
  const { ctx, errors } = await launch(site.port);
  const page = ctx.pages()[0] || (await ctx.newPage());
  const st = site.state;
  const mineActive = () => st.lots.filter((l) => l.mine && l.status === 'active');
  const sum = () => mineActive().reduce((a, l) => a + l.price, 0);

  try {
    // 1) страница ваших товаров: сайт грузит только первую страницу, расширение — все
    await page.goto('http://starpets.gg/profile/sales');
    await page.locator(`${P} .panel`).waitFor();
    const t0 = await waitFor('общая стоимость всех товаров', async () => {
      const t = await totals(page);
      return Math.abs(t.total - sum()) < 0.005 ? t : null;
    });
    assert.ok(Math.abs(t0.net - sum() * 0.8) < 0.006, `минус 20%: ${t0.net}`);
    const count = await text(page, '[data-f=counts]');
    assert.ok(count.includes(`Товаров: ${mineActive().length}`), count);
    console.log(`  ✓ сумма ${t0.total}, минус 20%: ${t0.net}, ${count}`);
    assert.match(await text(page, '[data-f=msg]'), /измените вручную цену/);
    await shot(page, `${variant}-1-totals`);
    // свернуть в «пилюлю» с суммами и развернуть обратно
    await page.locator(`${P} [data-act=collapse]`).click();
    assert.ok(await page.locator(`${P} .panel`).isHidden());
    const pill = await page.locator(`${P} .pill`).innerText();
    assert.ok(Math.abs(num(pill.split('\n')[1]) - t0.total) < 0.005 && Math.abs(num(pill.split('\n')[2]) - t0.net) < 0.005, pill);
    await page.locator(`${P} .pill`).click();
    assert.ok(await page.locator(`${P} .pill`).isHidden());

    // 2) один раз меняем цену вручную на сайте
    const row = page.locator('.sale').first();
    const before = num(await row.locator('.price-input').inputValue());
    await row.locator('.price-input').fill((before + 1).toFixed(2));
    await row.locator('.save').click();
    await waitFor('смена цены выучена', async () => /цены рынка|откройте на сайте страницу любого своего товара/i.test(await text(page, '[data-f=msg]')));
    const t1 = await waitFor('сумма после ручной смены', async () => {
      const t = await totals(page);
      return Math.abs(t.total - sum()) < 0.005 ? t : null;
    });
    console.log(`  ✓ смена цены выучена, сумма пересчитана: ${t1.total}`);

    // 3) открываем страницу своего товара с другими продавцами
    if (variant === 'a') await page.goto('http://starpets.gg/product/p-frost-dragon-nfr');
    else await page.goto('http://starpets.gg/catalog/frost-dragon?fly=1&ride=1&neon=1&mega=0');
    await page.locator(`${P} .panel`).waitFor();
    await waitFor('цены рынка выучены', async () => (await text(page, '[data-f=msg]')).trim() === '' || /Запомнил, где смотреть/.test(await text(page, '[data-f=msg]')));
    console.log(`  ✓ цены рынка выучены: ${await text(page, '[data-f=msg]')}`);

    // 4) снова свои товары: проверяем мин. цены и выбираем дороже рынка
    await page.goto('http://starpets.gg/profile/sales');
    await waitFor('товары загружены', async () => Math.abs((await totals(page)).total - sum()) < 0.005);
    const exp = expected(st);
    const toChange = [...exp.values()].filter((p) => p.action === 'change').length;
    await page.locator(`${P} [data-act=check]`).click();
    const msg = await waitFor('проверка цен рынка', async () => {
      const m = await text(page, '[data-f=msg]');
      return /Дороже минимальной цены рынка|уже стоят/.test(m) ? m : null;
    });
    assert.ok(msg.includes(`${toChange} шт.`), `${msg} (ждал ${toChange})`);
    await page.locator(`${P} [data-act=select-above]`).click();
    assert.equal(await page.locator(`${P} .item.sel`).count(), toChange);
    await shot(page, `${variant}-2-selected`);
    console.log(`  ✓ ${msg}`);

    // 5) ставим выбранные на минимальную цену
    const priceBefore = new Map(mineActive().map((l) => [l.item, l.price]));
    await page.locator(`${P} [data-act=apply-selected]`).click();
    const bigCount = [...exp.values()].filter((p) => p.big).length;
    const confirm = await modalRows(page);
    assert.equal(confirm.title, 'Цена сильно упадёт');
    assert.ok(confirm.body.includes('Turtle'), confirm.body);
    const keepBig = variant === 'a';
    await closeModal(page, keepBig ? 'Не трогать их' : 'Всё равно поставить');
    const res = await waitFor('мини-окно с результатом', async () => {
      const r = await modalRows(page);
      return /Цены изменены/.test(r.title) ? r : null;
    }, 60000);
    await shot(page, `${variant}-3-result`);
    console.log(`  мини-окно: ${res.title}\n    ${res.body.split('\n').filter(Boolean).slice(0, 6).join('\n    ')}`);
    const changedNow = toChange - (keepBig ? bigCount : 0);
    assert.equal(res.title, `Цены изменены: ${changedNow}`);

    for (const l of mineActive()) {
      const e = [...exp.entries()].find(([n]) => st.lots.find((x) => x.n === n).item === l.item)[1];
      const want = e.action === 'change' && !(keepBig && e.big) ? e.target : priceBefore.get(l.item);
      assert.ok(Math.abs(l.price - want) < 0.005, `${l.name} ${l.v}: на сайте ${l.price}, ждал ${want}`);
      if (e.action === 'change' && !(keepBig && e.big)) {
        assert.ok(res.body.includes(`${l.name}`), `в мини-окне нет ${l.name}`);
      }
    }
    const turtle = mineActive().find((l) => l.name === 'Turtle');
    assert.ok(res.body.includes('Turtle') === !keepBig || keepBig, 'Turtle в итогах');
    await closeModal(page);
    const t2 = await totals(page);
    assert.ok(Math.abs(t2.total - sum()) < 0.005, `сумма после смены: ${t2.total} / ${sum()}`);
    console.log(`  ✓ цены на «сайте» стали минимальными, сумма теперь ${t2.total}, минус 20%: ${t2.net}`);

    // 6) один товар кнопкой «мин.»
    if (keepBig) {
      const key = await page.locator(`${P} .item`, { hasText: 'Turtle' }).getAttribute('data-key');
      await page.locator(`${P} .item[data-key="${key}"] [data-act=apply-one]`).click();
      await modalRows(page);
      await closeModal(page, 'Всё равно поставить');
      const one = await waitFor('мини-окно для одного товара', async () => {
        const r = await modalRows(page);
        return r.title === 'Цена изменена' ? r : null;
      }, 30000);
      assert.match(one.body, /40,00 \$ → 15,00 \$/);
      assert.equal(turtle.price, 15);
      console.log(`  ✓ один товар: ${one.body.split('\n')[0]} ${one.body.split('\n')[1] || ''}`);
      await shot(page, `${variant}-4-one`);
      await closeModal(page);
    }
    const unicorn = await page.locator(`${P} .item`, { hasText: 'Unicorn' }).first().getAttribute('data-key');
    await page.locator(`${P} .item[data-key="${unicorn}"] [data-act=apply-one]`).click();
    const none = await waitFor('мини-окно без изменений', async () => modalRows(page), 30000);
    assert.equal(none.title, 'Цены не изменились');
    assert.match(none.body, /на рынке нет других продавцов/);
    await closeModal(page);
    console.log('  ✓ товар без других продавцов не тронут');

    // 7) ничего лишнего: чужие лоты и проданное не трогали, запросов покупки нет
    for (const l of st.lots.filter((x) => !x.mine || x.status === 'sold')) {
      assert.ok(l.status !== 'off', `чужой/проданный лот снят: ${l.n}`);
    }
    const writes = st.requests.filter((r) => r.method !== 'GET');
    assert.ok(writes.every((r) => /\/sales\/s-\d+$|\/offers(\/\d+)?$/.test(r.path)), JSON.stringify(writes.slice(-3)));
    assert.deepEqual(errors, []);
    console.log(`  ✓ запросов изменения: ${writes.length}, ошибок на странице нет`);
  } finally {
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${variant}-last.png`) }).catch(() => {});
    await ctx.close();
    await site.close();
  }
}

(async () => {
  if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
  for (const v of process.argv.slice(2).length ? process.argv.slice(2) : ['a', 'b']) await scenario(v);
  console.log('\nВсе проверки прошли.');
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
