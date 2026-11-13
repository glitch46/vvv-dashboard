// Run with: node --test scripts/test_unstaker_activity_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'unstaker_activity.js'), 'utf8');

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.classList = { toggle() {} };
    this.text = '';
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll() { return this.children; }
  addEventListener() {}
}

async function render(rankingDays, wallets) {
  const ids = ['unstakerStatus', 'unstakerWallets', 'unstakerSummary', 'unstakerHistory',
    'topUnstakersChart', 'unstakerEmpty', 'unstakerContext', 'unstakerWalletHistory',
    'unstakerWalletDetails', 'unstakerRankingDays'];
  const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
  const document = {
    getElementById: id => elements[id],
    createElement: () => new Element(),
    createTextNode: text => ({ textContent: text })
  };
  const data = { schema_version: 2, ranking_complete: true, ranking_days: rankingDays,
    history_scope: 'all', as_of: new Date().toISOString(), wallets };
  await vm.runInNewContext(source, {
    document, fetch: async () => ({ ok: true, json: async () => data }), console
  });
  return elements;
}

const wallet = { address: '0x' + '1'.repeat(40), initiated_amount: 500, status: 'indexing' };

test('wallet labels use the actual 30-day ranking', async () => {
  const elements = await render(30, [wallet]);
  assert.match(elements.unstakerStatus.textContent, /500 VVV initiated in the last 30 days/);
  assert.doesNotMatch(elements.unstakerStatus.textContent, /awaiting its first refresh/);
  assert.equal(elements.unstakerRankingDays.textContent, '30');
  assert.match(elements.unstakerWallets.attributes['aria-label'], /last 30 days/);
  assert.match(elements.unstakerWallets.children[0].title, /initiated in 30 days/);
});

test('previous seven-day snapshots stay correctly labeled during migration', async () => {
  const elements = await render(7, [wallet]);
  assert.match(elements.unstakerStatus.textContent, /initiated in the last 7 days/);
  assert.match(elements.unstakerStatus.textContent, /30-day ranking is awaiting its first refresh/);
  assert.equal(elements.unstakerRankingDays.textContent, '7');
  assert.match(elements.unstakerWallets.children[0].title, /initiated in 7 days/);
});

test('an empty snapshot describes the window actually scanned', async () => {
  const elements = await render(7, []);
  assert.match(elements.unstakerStatus.textContent, /last 7 days/);
  assert.match(elements.unstakerStatus.textContent, /30-day ranking is awaiting its first refresh/);
});
