// Load the built page, let its script render at the default rate, and save the rendered DOM
// (script kept) so the ranked list shows even where JavaScript doesn't run (iOS Files/Mail preview).
const { chromium } = require('playwright'); const fs = require('fs'); const path = require('path');
(async () => {
  const [src, out, shot] = process.argv.slice(2);
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  const errs = []; p.on('pageerror', e => errs.push(e.message)); p.on('console', m => m.type() === 'error' && errs.push(m.text()));
  p.on('request', r => { if (!r.url().startsWith('file:')) errs.push('EXTERNAL REQUEST ' + r.url()); });
  await p.goto('file://' + path.resolve(src));
  let html = await p.evaluate(() => '<!doctype html>\n' + document.documentElement.outerHTML);
  fs.writeFileSync(out, html);
  if (shot) await p.screenshot({ path: shot, fullPage: false });
  const top = await p.$$eval('.card', cs => cs.slice(0, 5).map(c => c.querySelector('h3').textContent + ' | ' + c.querySelector('.score b').textContent));
  console.log('cards', await p.$$eval('.card', c => c.length)); console.log(top.join('\n'));
  console.log('errors', errs);
  await b.close();
})();
