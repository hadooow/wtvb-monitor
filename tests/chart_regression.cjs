const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const html = fs.readFileSync('app/static/index.html', 'utf8');
const css = fs.readFileSync('app/static/styles.css', 'utf8');
const frames = [];
const context = vm.createContext({
  document: {createElement: () => ({style: {}, offsetWidth: 230}), querySelector: selector => context.controls[selector.slice(1)]},
  window: {devicePixelRatio: 2},
  requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
});
vm.runInContext(source.slice(0, source.indexOf('async function api(')), context);
vm.runInContext(source.slice(source.indexOf('function axisSeries('), source.indexOf('async function startFocus(')), context);
function canvas() {
  const listeners = new Map(), tooltips = [], strokes = [];
  const ctx = new Proxy({stroke: () => strokes.push(ctx.strokeStyle)}, {
    get: (target, name) => target[name] || (() => {}),
  });
  return {clientWidth: 640, clientHeight: 250, listeners, tooltips, strokes,
    getContext: () => ctx, closest: () => null,
    getBoundingClientRect: () => ({left: 100, top: 100, width: 640, height: 250}),
    parentElement: {append: tooltip => tooltips.push(tooltip), getBoundingClientRect: () => ({left: 85, top: 36})},
    addEventListener: (name, callback) => { assert(!listeners.has(name)); listeners.set(name, callback); },
  };
}
const start = Date.parse('2026-10-05T00:00:00Z'), end = start + 5 * 86400000;
const points = [
  {timestamp: new Date(start).toISOString(), velocity_x: 1, velocity_y: 2, velocity_z: 3},
  {timestamp: new Date(end).toISOString(), velocity_x: 7, velocity_y: null, velocity_z: 9},
];
const chart = canvas();
context.chart = chart; context.points = points;
context.options = {unit: 'mm/s', startTime: start, endTime: end};
const draw = () => vm.runInContext('drawChart(chart, points, axisSeries("velocity"), options)', context);
draw();
assert.equal(chart.width, 1280); // HiDPI backing pixels must not distort pointer time
assert.deepEqual([...new Set(chart.strokes)].sort(), ['#4385ff', '#ff4545', 'rgba(141,195,207,.13)'].sort());
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(axisColors)', context)), {x: '#ff4545', y: '#35d05b', z: '#4385ff'});
for (const [axis, color] of [['x', '#ff4545'], ['y', '#35d05b'], ['z', '#4385ff']]) {
  assert(css.includes(`--axis-${axis}: ${color}`));
  assert(css.includes(`.axis-${axis} { background: var(--axis-${axis}); }`));
}
const flush = () => { while (frames.length) frames.shift()(); };
chart.listeners.get('pointermove')({clientX: 100 + 53 + .9 * (626 - 53), clientY: 160});
chart.listeners.get('pointermove')({clientX: 100 + 53 + .9 * (626 - 53), clientY: 160});
assert.equal(frames.length, 1, 'pointer events coalesce to one drawing frame');
flush();
const tooltip = chart.tooltips[0];
assert.equal(tooltip.hidden, false);
assert(tooltip.textContent.includes(new Date(start + .9 * (end - start)).toLocaleString()));
assert(tooltip.textContent.includes(new Date(end).toLocaleString()));
assert(tooltip.textContent.includes('X：7.0 mm/s'));
assert(tooltip.textContent.includes('Y：— mm/s'), 'missing value must not appear as zero');
assert(tooltip.textContent.includes('Z：9.0 mm/s'));
for (let i = 0; i < 50; i++) draw();
assert.equal(chart.tooltips.length, 1);
assert.equal(chart.listeners.size, 2);
chart.listeners.get('pointerleave')(); flush();
assert.equal(tooltip.hidden, true);
chart.listeners.get('pointermove')({clientX: 120, clientY: 160}); flush();
assert.equal(tooltip.hidden, true, 'left axis area is outside data plot');
context.points = [];
draw();
chart.listeners.get('pointermove')({clientX: 400, clientY: 160}); flush();
assert(tooltip.textContent.includes('此范围内暂无样本'));
assert(!tooltip.textContent.includes('X：'));
assert.equal(vm.runInContext('nearestChartPoint([], 0)', context), null);
assert.equal(vm.runInContext('chartValue(undefined)', context), null);
assert.equal(vm.runInContext('chartValue(0)', context), 0);

const presets = [1, 5, 10, 30, 60, 180, 360, 1440, 4320, 7200, 10080];
const liveSelect = html.match(/<select id="liveRangePreset">([\s\S]*?)<\/select>/)[1];
for (const minutes of presets) {
  assert(liveSelect.includes(`value="${minutes}"`));
  assert(html.includes(`data-history-minutes="${minutes}"`));
}
context.clearTimeout = () => {};
context.controls = {rangeMinutes: {}, liveRangePreset: {options: presets.map(value => ({value: String(value)}))}};
context.loaded = [];
vm.runInContext('let rangeTimer; loadHistory = async () => loaded.push(state.rangeMinutes); renderMonitor = () => {};', context);
vm.runInContext(source.slice(source.indexOf('async function setLiveRange('), source.indexOf('$("#liveRangePreset").addEventListener')), context);
(async () => {
  for (const minutes of presets) {
    await vm.runInContext(`setLiveRange(${minutes})`, context);
    assert.equal(context.controls.rangeMinutes.value, minutes);
    assert.equal(context.controls.liveRangePreset.value, String(minutes));
  }
  await vm.runInContext('setLiveRange(17)', context);
  assert.equal(context.controls.liveRangePreset.value, '');
  await vm.runInContext('setLiveRange(50000)', context);
  assert.equal(context.controls.rangeMinutes.value, 10080);
  console.log('RGB axes, hover timestamps/nearest samples, HiDPI, missing/empty values, listener reuse, and all time presets passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
