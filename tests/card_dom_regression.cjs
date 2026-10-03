// Keep mouse targets alive through many live updates, including status changes.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app/static/app.js', 'utf8');
class Node {
  constructor() {
    this.dataset = new Proxy({}, {set(object, key, value) { object[key] = String(value); return true; }});
    this.textContent = ''; this.hidden = false;
    this.style = {};
  }
}
class Card extends Node {
  set innerHTML(html) {
    this.fields = new Map([...html.matchAll(/data-field="([^"]+)"/g)].map(m => [m[1], new Node()]));
    this.tag = new Node();
    this.buttons = [...html.matchAll(/<button\b/g)].map(() => new Node());
  }
  querySelector(selector) {
    return selector === '.tag' ? this.tag : this.fields.get(selector.match(/data-field="([^"]+)"/)[1]);
  }
  remove() { grid.children = grid.children.filter(c => c !== this); }
}
const grid = {
  children: [],
  append(card) { this.children.push(card); },
  querySelectorAll() { return this.children; },
  querySelector(selector) {
    if (selector === '.empty-state') return null;
    return this.children.find(c => c.dataset.deviceId == selector.match(/data-device-id="([^"]+)"/)[1]);
  },
};
const context = vm.createContext({document: {querySelector: () => grid, createElement: () => new Card()}, fixture: null});
vm.runInContext(source.slice(0, source.indexOf('async function openMonitor(')), context);
const device = (id, status = 'connected') => ({id, name: `<sensor ${id}>`, mac: String(id), location: '', enabled: true,
  runtime: {status, collecting: true, handle: id, manual_paused: false, latest: {temperature: 25, timestamp: '2026-10-01T04:00:00Z'}}});
const render = devices => {
  context.fixture = devices;
  vm.runInContext('state.dashboard = {devices: fixture, queue_order: []}; renderCards(fixture);', context);
};
render([device(1), device(2)]);
const card = grid.children[0], buttons = [...card.buttons], field = card.fields.get('temperature');
for (let i = 0; i < 1000; i++) {
  const first = device(1, i % 2 ? 'paused' : 'connected');
  first.runtime.latest.temperature = 25 + i / 100;
  first.runtime.manual_paused = Boolean(i % 2);
  render([device(2), first]);
  assert.equal(grid.children[0], card);
  assert.equal(card.fields.get('temperature'), field);
  assert.deepEqual(card.buttons, buttons);
}
assert.equal(card.fields.get('name').textContent, '<sensor 1>');
render([device(2), device(3)]);
assert.deepEqual(grid.children.map(c => Number(c.dataset.deviceId)), [2, 3]);
console.log('Stable card/button identity, value updates, and registration changes passed');
const named = (id, name, status) => ({ ...device(id, status), name });
const preview = [named(10, 'cbf10', 'connected'), named(11, 'WTVB', 'connected'),
  named(12, 'CBF2', 'connected'), named(13, 'B-sensor', 'connecting'),
  named(14, 'A-sensor', 'retrying'), named(15, 'Z-sensor', 'disabled')];
preview[0].runtime.handle = 1;
preview[2].runtime.handle = 99; // connection handle must not determine name order
const visualOrder = () => [...grid.children].sort((a, b) => Number(a.style.order) - Number(b.style.order))
  .map(card => Number(card.dataset.deviceId));
render(preview);
assert.deepEqual(visualOrder(), [12, 10, 11, 14, 13, 15]);
const savedCards = new Map(grid.children.map(card => [card.dataset.deviceId, card]));
for (let i = 0; i < 100; i++) {
  render([...preview].reverse());
  assert.deepEqual(visualOrder(), [12, 10, 11, 14, 13, 15]);
  for (const card of grid.children) assert.equal(card, savedCards.get(card.dataset.deviceId));
}
preview[2].runtime.status = 'paused';
render(preview);
assert.deepEqual(visualOrder(), [10, 11, 14, 13, 12, 15]);
preview[2].runtime.status = 'connected';
preview[0].name = 'Alpha';
render(preview);
assert.deepEqual(visualOrder(), [10, 12, 11, 14, 13, 15]);
// Restore the connection button fixture used below.
render([device(2), device(3)]);
console.log('Connected-first alphabetical preview order, numeric names, status changes, and rename passed');
let release;
context.pendingResponse = new Promise(resolve => { release = resolve; });
context.requests = [];
context.alert = message => { throw new Error(message); };
vm.runInContext(source.slice(source.indexOf('async function toggleDeviceConnection('), source.indexOf('async function switchMonitorTab(')), context);
vm.runInContext('render = () => {}; loadDashboard = async () => {}; api = path => { requests.push(path); return pendingResponse; };', context);
const first = vm.runInContext('toggleDeviceConnection(2)', context);
const duplicate = vm.runInContext('toggleDeviceConnection(2)', context);
assert.deepEqual(context.requests, ['/api/devices/2/disconnect']);
release({ok: true});
Promise.all([first, duplicate]).then(() => {
  assert.equal(vm.runInContext('state.connectionRequests.size', context), 0);
  console.log('Overlapping connection clicks submit exactly one request');
}).catch(error => { console.error(error); process.exitCode = 1; });
