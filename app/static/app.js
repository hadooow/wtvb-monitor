const state = {
  dashboard: null,
  focusMac: null,
  monitorDeviceId: null,
  rangeMinutes: 1,
  activeMonitorTab: "live",
  historyStartMs: null,
  historyEndMs: null,
  historyPoints: [],
  historyPage: 1,
  historyTotal: 0,
  series: new Map(),
  socket: null,
  historyRequest: 0,
  deleteDeviceId: null,
  connectionRequests: new Set(),
};

const statusNames = {
  connected: "采集中",
  connecting: "连接中",
  disconnecting: "切换中",
  paused: "已手动断开",
  queued: "排队中",
  retrying: "重试中",
  disabled: "已停用",
};
const axisColors = { x: "#55a7ff", y: "#ffbd59", z: "#ba8cff" };
const $ = selector => document.querySelector(selector);
const fmt = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits);
const maxAxis = (sample, prefix) => {
  const values = ["x", "y", "z"].map(axis => sample?.[`${prefix}_${axis}`]).filter(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
  return values.length ? Math.max(...values.map(value => Math.abs(Number(value)))) : null;
};

function errorText(value) {
  if (!value) return "";
  if (value.includes("SERIAL_BLUETOOTH_PORT")) return "所选 COM 口是蓝牙虚拟串口，请选择 USB 网关端口或切换电脑蓝牙直连";
  if (value.includes("SERIAL_PORT_SELECTION")) return "请在采集设置中选择实际 USB 网关串口";
  if (value.includes("BLE_SERVICE_MISMATCH")) return "电脑已连接，但未找到 FFE5/FFE4 数据服务，请下载诊断核对";
  if (value.includes("BLE_CONNECT_FAILED")) return `电脑蓝牙连接失败 · ${value}`;
  if (value.includes("SERIAL_IO_ERROR")) return "串口读取或写入失败，正在自动重连网关";
  const profile = value.includes(" | ") ? ` · 已尝试${value.split(" | ").slice(1).join(" | ")}` : "";
  if (value.includes("CNN_BUSY")) return `网关正忙，已自动排队重试${profile}`;
  if (value.includes("SERIAL_COLLISION_SUSPECTED")) return "连接指令期间检测到串口干扰，正在同步网关连接状态";
  if (value.includes("AT_RESPONSE_TIMEOUT")) return "网关回复不完整，正在同步网关连接状态";
  if (value.includes("AT_RESYNC_FAILED")) return "网关连接状态连续同步失败，请检查串口链路";
  if (value.includes("AT_ERROR")) return `网关拒绝指令，请下载日志查看详情${profile}`;
  if (value.includes("TIMEOUT")) return `蓝牙连接超时，请检查供电、Type-C 线及手机连接${profile}`;
  if (value.includes("DISSCONNECT") && value.includes("34")) return `无线链路响应超时（BLE 34）${profile}`;
  if (value.includes("DISSCONNECT")) return `传感器主动断开或链路中断${profile}`;
  if (value.includes("SERVICE_NOT_FOUND")) return `未找到传感器数据服务${profile}`;
  if (value.includes("CCCD_ERROR")) return `无法开启传感器数据通知${profile}`;
  return value;
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    let message = "请求失败";
    try { message = (await response.json()).detail || message; } catch (_) { /* response was not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

async function loadDashboard() {
  state.dashboard = await api("/api/dashboard");
  state.focusMac = state.dashboard.focus_mac;
  render();
}

function render() {
  if (!state.dashboard) return;
  const { devices, gateway, settings } = state.dashboard;
  const badge = $("#gatewayBadge");
  badge.textContent = gateway.error
    ? `网关异常 · ${errorText(gateway.error)}`
    : `${gateway.name} · ${gateway.driver === "ble" ? (gateway.online ? (gateway.scanning ? "正在扫描" : "直连驱动运行中") : "直连驱动未启动") : gateway.driver === "simulator" ? "模拟模式" : gateway.online ? (gateway.last_response_seconds_ago === null ? "串口已打开 · 等待网关回复" : "串口已打开 · 已收到网关回复") : "串口未打开"}`;
  badge.classList.toggle("error", Boolean(gateway.error));
  const warning = $("#gatewayWarning");
  const warnings = [];
  if (gateway.ble_scan_error) warnings.push(`电脑蓝牙扫描失败：${gateway.ble_scan_error}。请开启蓝牙并使用支持 BLE 的适配器`);
  if (gateway.adaptive_warning) warnings.push(gateway.adaptive_warning);
  if (gateway.data_recovery_seconds > 0) warnings.push(`连接操作刚结束，正在观察数据恢复（约 ${Math.ceil(gateway.data_recovery_seconds)} 秒）`);
  if (gateway.io_failed) warnings.push(`串口自动重连等待约 ${Math.ceil(gateway.automatic_reconnect_seconds || 0)} 秒`);
  if (gateway.scan_start_failures && !gateway.faulted) warnings.push(`网关暂时拒绝启动扫描（第 ${gateway.scan_start_failures} 次），正在同步连接状态，稍后重试`);
  if (gateway.warning_count) warnings.push(`已丢弃 ${gateway.warning_count} 条损坏或不支持的数据通知。最近一次：${gateway.last_warning?.time ? new Date(gateway.last_warning.time).toLocaleTimeString() : "见诊断日志"}。详情见诊断日志。`);
  warning.hidden = !warnings.length;
  warning.textContent = warnings.join("。 ");
  const connected = devices.filter(device => device.runtime.status === "connected").length;
  $("#connectedCount").textContent = connected;
  $("#connectionLimit").textContent = `/ ${gateway.maximum ?? settings.max_connections} 路连接`;
  $("#deviceCount").textContent = devices.length;
  $("#collectingCount").textContent = devices.filter(device => device.runtime.collecting).length;
  $("#queuedCount").textContent = devices.filter(device => ["queued", "connecting", "disconnecting"].includes(device.runtime.status)).length;
  $("#errorCount").textContent = devices.filter(device => device.runtime.status === "retrying").length;
  renderCards(devices);
  if ($("#monitorDialog").open) renderMonitor();
}

function renderCards(devices) {
  const grid = $("#deviceGrid");
  const registered = new Set(devices.map(device => String(device.id)));
  grid.querySelectorAll("[data-device-id]").forEach(card => {
    if (!registered.has(card.dataset.deviceId)) card.remove();
  });
  if (!devices.length) {
    if (!grid.querySelector(".empty-state")) grid.innerHTML = '<div class="empty-state"><strong>尚未登记设备</strong><span>点击右上角“添加设备”开始配置传感器。</span></div>';
    return;
  }
  grid.querySelector(".empty-state")?.remove();
  const queueRank = new Map((state.dashboard?.queue_order || []).map((mac, index) => [mac, index]));
  const statusRank = { connected: 0, connecting: 1, disconnecting: 2, queued: 3, retrying: 3, paused: 4, disabled: 5 };
  const ordered = [...devices].sort((a, b) => {
    const aRank = statusRank[a.runtime.status] ?? 5;
    const bRank = statusRank[b.runtime.status] ?? 5;
    if (aRank !== bRank) return aRank - bRank;
    if (aRank === 0) return (a.runtime.handle ?? a.id) - (b.runtime.handle ?? b.id);
    if (aRank === 3) return (queueRank.get(a.mac) ?? Infinity) - (queueRank.get(b.mac) ?? Infinity);
    return a.id - b.id;
  });
  ordered.forEach(device => {
    const runtime = device.runtime;
    const live = Boolean(runtime.collecting);
    const sample = runtime.latest;
    const alarm = live ? (runtime.alarm || { level: "normal", reasons: [] }) : { level: "normal", reasons: [] };
    const badgeClass = alarm.level !== "normal" ? alarm.level : runtime.status;
    const connectionLabel = runtime.status === "connected" && !live ? "已连接 · 等待数据" : statusNames[runtime.status] || runtime.status;
    const queuePosition = queueRank.has(device.mac) ? ` · 排队第 ${queueRank.get(device.mac) + 1} 位` : "";
    const badgeText = alarm.level === "alarm" ? `${connectionLabel} · 报警` : alarm.level === "warning" ? `${connectionLabel} · 预警` : runtime.is_focus && live ? `${connectionLabel} · 实时优先` : runtime.is_focus ? `${connectionLabel} · 已优先` : `${connectionLabel}${queuePosition}`;
    const alarmReason = alarm.reasons?.map(item => `${item.label} ${fmt(item.value)} ${item.unit}`).join(" · ") || "";
    const reason = alarmReason || (runtime.error ? `连接失败：${errorText(runtime.error)}` : "");
    const timeText = sample ? `${live ? "实时数据" : "最近有效值 · 当前未采集"} ${new Date(sample.timestamp).toLocaleTimeString()}` : runtime.status === "connecting" ? "正在连接，等待首条数据" : runtime.last_discovered_seconds_ago == null && device.enabled && !runtime.manual_paused ? "尚未扫描到，请检查供电与距离" : "等待有效数据";
    const rssiText = runtime.rssi !== null && runtime.rssi !== undefined ? ` · ${runtime.rssi} dBm` : "";
    let card = grid.querySelector(`[data-device-id="${device.id}"]`);
    if (!card) {
      card = document.createElement("article");
      card.dataset.deviceId = device.id;
      card.innerHTML = `
      <div class="device-head">
        <div><h3><span data-field="name"></span><span class="tag">模拟</span></h3><p data-field="location"></p></div>
        <span data-field="status" class="status"></span>
      </div>
      <div class="card-values">
        <div><span>温度</span><strong data-field="temperature"></strong></div>
        <div><span>最大速度</span><strong data-field="velocity"></strong></div>
        <div><span>最大位移</span><strong data-field="displacement"></strong></div>
        <div><span>最大频率</span><strong data-field="frequency"></strong></div>
      </div>
      <div class="alarm-reason" data-field="reason"></div>
      <div class="device-foot">
        <span data-field="time"></span>
        <div class="card-actions">
          <button type="button" class="secondary" onclick="openDeviceSettings(${device.id})">设置</button>
          <button type="button" data-field="connection" class="secondary" onclick="toggleDeviceConnection(${device.id})"></button>
          <button type="button" class="primary" onclick="openMonitor(${device.id})">实时监控</button>
        </div>
      </div>`;
      // Existing cards keep their nodes and positions during live updates.
      // Replacing or moving a hovered button can cancel a mouse click.
      grid.append(card);
    }
    const text = (field, value) => {
      const node = card.querySelector(`[data-field="${field}"]`);
      if (node.textContent !== value) node.textContent = value;
      return node;
    };
    card.className = `device-card ${runtime.is_focus ? "focus" : ""} ${alarm.level}`;
    text("name", device.name);
    card.querySelector(".tag").hidden = !device.simulated;
    text("location", device.location || device.mac_display);
    text("status", badgeText).className = `status ${badgeClass}`;
    text("temperature", `${fmt(sample?.temperature)} °C`);
    text("velocity", `${fmt(maxAxis(sample, "velocity"), 0)} mm/s`);
    text("displacement", `${fmt(maxAxis(sample, "displacement"), 0)} μm`);
    text("frequency", `${fmt(maxAxis(sample, "frequency"), 0)} Hz`);
    text("reason", reason);
    text("time", `${timeText}${rssiText}`);
    const connection = text("connection", runtime.manual_paused ? "重新连接" : "断开");
    connection.hidden = !device.enabled;
    connection.disabled = state.connectionRequests.has(device.id);
  });
}

async function openMonitor(id) {
  const device = state.dashboard.devices.find(item => item.id === id);
  if (!device) return;
  state.monitorDeviceId = id;
  state.activeMonitorTab = "live";
  state.historyStartMs = null;
  state.historyEndMs = null;
  state.historyPoints = [];
  state.historyPage = 1;
  document.querySelectorAll("[data-monitor-tab]").forEach(button => button.classList.toggle("active", button.dataset.monitorTab === "live"));
  $("#livePanel").classList.remove("hidden");
  $("#historyPanel").classList.add("hidden");
  $("#monitorDialog").showModal();
  renderMonitor();
  await loadHistory();
}

async function loadHistory() {
  const id = state.monitorDeviceId;
  if (!id) return;
  const request = ++state.historyRequest;
  try {
    const history = await api(`/api/devices/${id}/history?minutes=${state.rangeMinutes}&limit=20000`);
    if (request !== state.historyRequest || id !== state.monitorDeviceId) return;
    const device = state.dashboard.devices.find(item => item.id === id);
    if (!device) return;
    const livePoints = state.series.get(device.mac) || [];
    state.series.set(device.mac, mergePoints(history, livePoints));
    renderMonitor();
  } catch (error) {
    $("#monitorMessage").textContent = `历史数据读取失败：${error.message}`;
  }
}

function localDateTimeValue(timestamp) {
  const local = new Date(timestamp - new Date(timestamp).getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function setHistoryRange(minutes) {
  state.historyEndMs = Date.now();
  state.historyStartMs = state.historyEndMs - minutes * 60000;
  state.historyPage = 1;
  $("#historyStart").value = localDateTimeValue(state.historyStartMs);
  $("#historyEnd").value = localDateTimeValue(state.historyEndMs);
  document.querySelectorAll("[data-history-minutes]").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.historyMinutes) === minutes);
  });
}

function historyQuery() {
  if (state.historyStartMs === null || state.historyEndMs === null) setHistoryRange(60);
  return new URLSearchParams({
    start: new Date(state.historyStartMs).toISOString(),
    end: new Date(state.historyEndMs).toISOString(),
  });
}

async function loadHistoricalRange() {
  const id = state.monitorDeviceId;
  if (!id) return;
  const request = ++state.historyRequest;
  const query = historyQuery();
  $("#historyMessage").textContent = "正在读取历史数据…";
  try {
    const [points, table] = await Promise.all([
      api(`/api/devices/${id}/history?${new URLSearchParams({ ...Object.fromEntries(query), max_points: 2000 })}`),
      api(`/api/devices/${id}/history/table?${new URLSearchParams({ ...Object.fromEntries(query), page: state.historyPage, page_size: 100 })}`),
    ]);
    if (request !== state.historyRequest || id !== state.monitorDeviceId) return;
    state.historyPoints = points;
    state.historyTotal = table.total;
    renderHistoryTable(table.items);
    renderHistoryCharts();
    $("#historyMessage").textContent = `趋势图显示 ${points.length.toLocaleString()} 个抽样点；此时间范围共有 ${table.total.toLocaleString()} 条采集记录。`;
  } catch (error) {
    if (request === state.historyRequest) $("#historyMessage").textContent = `历史数据读取失败：${error.message}`;
  }
}

function renderHistoryTable(rows) {
  $("#historyTableBody").innerHTML = rows.length ? rows.map(row => `
    <tr><td>${escapeHtml(new Date(row.timestamp).toLocaleString())}</td>
    <td>${fmt(row.temperature)}</td>
    <td>${fmt(row.velocity_x)} / ${fmt(row.velocity_y)} / ${fmt(row.velocity_z)}</td>
    <td>${fmt(row.displacement_x, 0)} / ${fmt(row.displacement_y, 0)} / ${fmt(row.displacement_z, 0)}</td>
    <td>${fmt(row.frequency_x, 0)} / ${fmt(row.frequency_y, 0)} / ${fmt(row.frequency_z, 0)}</td></tr>`).join("")
    : '<tr><td colspan="5">所选时间范围内暂无记录</td></tr>';
  $("#historyTableCount").textContent = `${state.historyTotal.toLocaleString()} 条记录`;
  const pages = Math.max(1, Math.ceil(state.historyTotal / 100));
  $("#historyPageLabel").textContent = `第 ${state.historyPage} / ${pages} 页`;
  $("#historyPrevious").disabled = state.historyPage <= 1;
  $("#historyNext").disabled = state.historyPage >= pages;
}

function renderHistoryCharts() {
  const device = state.dashboard?.devices.find(item => item.id === state.monitorDeviceId);
  if (!device) return;
  const startTime = state.historyStartMs;
  const endTime = state.historyEndMs;
  const thresholds = device.thresholds || {};
  drawChart($("#historyTemperature"), state.historyPoints, [{ key: "temperature", color: "#31dfc4" }], { unit: "°C", temperature: true, thresholds, startTime, endTime });
  drawChart($("#historyVelocity"), state.historyPoints, axisSeries("velocity"), { unit: "mm/s", startTime, endTime });
  drawChart($("#historyDisplacement"), state.historyPoints, axisSeries("displacement"), { unit: "μm", startTime, endTime });
  drawChart($("#historyFrequency"), state.historyPoints, axisSeries("frequency"), { unit: "Hz", startTime, endTime });
}

async function toggleDeviceConnection(id) {
  const device = state.dashboard?.devices.find(item => item.id === id);
  if (!device || state.connectionRequests.has(id)) return;
  state.connectionRequests.add(id);
  render();
  try {
    const action = device.runtime.manual_paused ? "reconnect" : "disconnect";
    await api(`/api/devices/${id}/${action}`, { method: "POST" });
    await loadDashboard();
  } catch (error) { alert(error.message); }
  finally {
    state.connectionRequests.delete(id);
    render();
  }
}

async function switchMonitorTab(tab) {
  state.activeMonitorTab = tab;
  document.querySelectorAll("[data-monitor-tab]").forEach(button => button.classList.toggle("active", button.dataset.monitorTab === tab));
  $("#livePanel").classList.toggle("hidden", tab !== "live");
  $("#historyPanel").classList.toggle("hidden", tab !== "history");
  if (tab === "history") {
    if (state.historyStartMs === null || state.historyEndMs === null) setHistoryRange(60);
    await loadHistoricalRange();
  } else if (!state.series.has(state.dashboard.devices.find(item => item.id === state.monitorDeviceId)?.mac)) {
    await loadHistory();
  }
}

function mergePoints(first, second) {
  const unique = new Map();
  [...first, ...second].forEach(point => {
    if (point?.timestamp) unique.set(point.timestamp, point);
  });
  return [...unique.values()].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)).slice(-20000);
}

function renderMonitor() {
  const device = state.dashboard?.devices.find(item => item.id === state.monitorDeviceId);
  if (!device) return;
  const runtime = device.runtime;
  const live = Boolean(runtime.collecting);
  const sample = runtime.latest;
  const focused = state.focusMac === device.mac;
  $("#monitorTitle").textContent = device.name;
  $("#monitorMeta").textContent = `${device.mac_display} · ${device.location || "未填写安装位置"} · ${statusNames[runtime.status] || runtime.status}`;
  $("#startFocusButton").classList.toggle("hidden", focused || runtime.manual_paused || !device.enabled);
  $("#stopFocusButton").classList.toggle("hidden", !focused);
  $("#deviceConnectionButton").classList.toggle("hidden", !device.enabled);
  $("#deviceConnectionButton").textContent = runtime.manual_paused ? "重新连接" : "断开连接";
  const alarmText = live && runtime.alarm?.reasons?.length
    ? runtime.alarm.reasons.map(item => `${item.label} ${fmt(item.value)} ${item.unit}（阈值 ${fmt(item.threshold)}）`).join("；")
    : "";
  $("#monitorMessage").textContent = (!live && sample ? `当前未采集，以下为最近有效值（${new Date(sample.timestamp).toLocaleString()}）。` : "") + (alarmText || (runtime.manual_paused
    ? "该设备已手动断开；点击“重新连接”后将重新加入调度。此操作不会更改设备启用设置。"
    : focused
    ? `${statusNames[runtime.status] || runtime.status} · 当前设备拥有最高连接优先级，页面关闭后将自动释放`
    : `${statusNames[runtime.status] || runtime.status} · 点击“优先连接并实时监控”可优先占用一个连接通道`));
  renderDiagnostics(runtime);
  $("#temperatureNow").textContent = `${fmt(sample?.temperature)} °C`;
  renderAxisValues("velocityNow", sample, "velocity", "mm/s", 0);
  renderAxisValues("displacementNow", sample, "displacement", "μm", 0);
  renderAxisValues("frequencyNow", sample, "frequency", "Hz", 0);
  renderExtraValues(sample);
  drawAllCharts(device);
  if (state.activeMonitorTab === "history") renderHistoryCharts();
}

function renderDiagnostics(runtime) {
  const panel = $("#diagnosticPanel");
  const gateway = state.dashboard.gateway;
  const realMode = ["serial", "ble"].includes(gateway.driver);
  panel.classList.toggle("hidden", !realMode);
  if (!realMode) return;
  const seen = runtime.last_discovered_seconds_ago !== null && runtime.last_discovered_seconds_ago !== undefined;
  const rssi = runtime.rssi;
  const weak = rssi !== null && rssi !== undefined && rssi < -75;
  panel.innerHTML = `
    <div><span>${gateway.driver === "ble" ? "电脑蓝牙" : "485 / USB 网关"}</span><strong>${gateway.driver === "ble" ? (gateway.online ? "直连驱动运行中" : "直连驱动未启动") : gateway.online ? (gateway.last_response_seconds_ago === null ? "串口已打开，尚无回复" : `最近回复 ${gateway.last_response_seconds_ago} 秒前`) : "串口未打开"}</strong></div>
    <div class="${weak ? "weak" : ""}"><span>传感器广播</span><strong>${seen ? `已发现 · ${rssi ?? "—"} dBm${weak ? " · 信号弱" : ""}` : "尚未扫描到"}</strong></div>
    <div class="${runtime.error ? "failed" : ""}"><span>BLE 数据连接</span><strong>${runtime.status === "connected" ? (runtime.collecting ? "已连接并接收有效数据" : "已连接，等待有效数据") : runtime.status === "connecting" ? "正在建立连接" : escapeHtml(errorText(runtime.error)) || statusNames[runtime.status] || "等待连接"}</strong></div>
    <div><span>有效数据</span><strong>${runtime.last_sample_seconds_ago != null ? `最近有效样本 ${runtime.last_sample_seconds_ago} 秒前` : "尚未收到有效样本"}${runtime.status === "retrying" ? ` · ${Math.ceil(runtime.retry_seconds || 0)} 秒后重试` : ""}</strong></div>
    ${gateway.driver === "serial" ? `<div><span>串口诊断</span><strong>乱码 ${gateway.non_ascii_bytes ?? 0} 字节 · 疑似碰撞 ${gateway.serial_collision_suspected ?? 0} 次</strong></div>` : ""}`;
}

function renderAxisValues(elementId, sample, prefix, unit, digits) {
  $("#" + elementId).innerHTML = ["x", "y", "z"].map(axis =>
    `<span style="color:${axisColors[axis]}">${axis.toUpperCase()} <strong>${fmt(sample?.[`${prefix}_${axis}`], digits)}</strong> ${unit}</span>`
  ).join("");
}

function renderExtraValues(sample) {
  const rows = [
    ["振动角 X", sample?.vibration_angle_x, "°", 2], ["振动角 Y", sample?.vibration_angle_y, "°", 2], ["振动角 Z", sample?.vibration_angle_z, "°", 2],
    ["加速度 X", sample?.acceleration_x, "g", 3], ["加速度 Y", sample?.acceleration_y, "g", 3], ["加速度 Z", sample?.acceleration_z, "g", 3],
    ["角速度 X", sample?.angular_velocity_x, "°/s", 2], ["角速度 Y", sample?.angular_velocity_y, "°/s", 2], ["角速度 Z", sample?.angular_velocity_z, "°/s", 2],
    ["电量", sample?.battery, "%", 0],
  ];
  $("#detailValues").innerHTML = rows.map(([label, value, unit, digits]) =>
    `<div class="detail-item"><span>${label}</span><strong>${fmt(value, digits)} ${unit}</strong></div>`
  ).join("");
}

function acceptSample(mac, sample, runtime) {
  const device = state.dashboard?.devices.find(item => item.mac === mac);
  if (!device) return;
  device.runtime = { ...device.runtime, ...runtime, latest: sample };
  const points = state.series.get(mac) || [];
  points.push(sample);
  if (points.length > 20000) points.splice(0, points.length - 20000);
  state.series.set(mac, points);
  renderCards(state.dashboard.devices);
  if ($("#monitorDialog").open && state.monitorDeviceId === device.id) renderMonitor();
}

function visiblePoints(mac) {
  const cutoff = Date.now() - state.rangeMinutes * 60000;
  return (state.series.get(mac) || []).filter(point => new Date(point.timestamp).getTime() >= cutoff);
}

function drawAllCharts(device) {
  const points = visiblePoints(device.mac);
  const thresholds = device.thresholds || {};
  drawChart($("#chartTemperature"), points, [{ key: "temperature", color: "#31dfc4" }], { unit: "°C", temperature: true, thresholds });
  drawChart($("#chartVelocity"), points, axisSeries("velocity"), { unit: "mm/s" });
  drawChart($("#chartDisplacement"), points, axisSeries("displacement"), { unit: "μm" });
  drawChart($("#chartFrequency"), points, axisSeries("frequency"), { unit: "Hz" });
}

function axisSeries(prefix) {
  return ["x", "y", "z"].map(axis => ({ key: `${prefix}_${axis}`, color: axisColors[axis] }));
}

function drawChart(canvas, points, series, options) {
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 640;
  const height = canvas.clientHeight || 250;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const plot = { left: 53, right: width - 14, top: 14, bottom: height - 32 };
  const endTime = options.endTime ?? Date.now();
  const startTime = options.startTime ?? (endTime - state.rangeMinutes * 60000);
  const rangeMinutes = (endTime - startTime) / 60000;
  const allValues = [];
  for (const point of points) for (const item of series) {
    const value = Number(point[item.key]);
    if (Number.isFinite(value)) allValues.push(value);
  }

  ctx.font = '10px "Segoe UI", sans-serif';
  ctx.strokeStyle = "rgba(141,195,207,.13)";
  ctx.fillStyle = "#789399";
  ctx.lineWidth = 1;
  let min = allValues.length ? Math.min(...allValues) : 0;
  let max = allValues.length ? Math.max(...allValues) : 1;
  if (min === max) { const padding = Math.max(Math.abs(min) * .08, 1); min -= padding; max += padding; }
  else { const padding = (max - min) * .1; min -= padding; max += padding; }

  for (let i = 0; i <= 4; i++) {
    const y = plot.top + (plot.bottom - plot.top) * i / 4;
    ctx.beginPath(); ctx.moveTo(plot.left, y); ctx.lineTo(plot.right, y); ctx.stroke();
    const label = max - (max - min) * i / 4;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(formatScale(label), plot.left - 7, y);
  }
  for (let i = 0; i <= 4; i++) {
    const x = plot.left + (plot.right - plot.left) * i / 4;
    const timestamp = startTime + (endTime - startTime) * i / 4;
    ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center";
    ctx.textBaseline = "top";
    ctx.fillText(formatAxisTime(timestamp, rangeMinutes), x, plot.bottom + 9);
  }

  if (!allValues.length) {
    ctx.fillStyle = "#8fa9ad";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("所选时间范围内暂无数据", (plot.left + plot.right) / 2, (plot.top + plot.bottom) / 2);
    return;
  }

  const xOf = point => plot.left + Math.max(0, Math.min(1, (new Date(point.timestamp).getTime() - startTime) / (endTime - startTime))) * (plot.right - plot.left);
  const yOf = value => plot.bottom - (value - min) / (max - min) * (plot.bottom - plot.top);
  for (const item of series) {
    let previous = null;
    for (const point of points) {
      const value = Number(point[item.key]);
      if (!Number.isFinite(value)) { previous = null; continue; }
      const current = { x: xOf(point), y: yOf(value), value };
      if (previous) {
        ctx.beginPath(); ctx.moveTo(previous.x, previous.y); ctx.lineTo(current.x, current.y);
        ctx.strokeStyle = options.temperature
          ? temperatureColor((previous.value + current.value) / 2, min, max, options.thresholds)
          : item.color;
        ctx.lineWidth = 2; ctx.stroke();
      }
      previous = current;
    }
  }
}

function temperatureColor(value, min, max, thresholds) {
  if (thresholds.temperature_alarm !== undefined && value >= Number(thresholds.temperature_alarm)) return "#ff6f69";
  if (thresholds.temperature_warn !== undefined && value >= Number(thresholds.temperature_warn)) return "#ffbd59";
  const ratio = max === min ? .5 : (value - min) / (max - min);
  if (ratio < .34) return "#55a7ff";
  if (ratio > .67) return "#ffbd59";
  return "#31dfc4";
}

function formatScale(value) {
  const abs = Math.abs(value);
  return abs >= 100 ? value.toFixed(0) : abs >= 10 ? value.toFixed(1) : value.toFixed(2);
}

function formatAxisTime(timestamp, rangeMinutes = state.rangeMinutes) {
  const date = new Date(timestamp);
  if (rangeMinutes > 1440) return `${date.getMonth() + 1}/${date.getDate()} ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: rangeMinutes <= 5 ? "2-digit" : undefined });
}

async function startFocus() {
  const device = state.dashboard.devices.find(item => item.id === state.monitorDeviceId);
  if (!device) return;
  try {
    const result = await api(`/api/devices/${device.id}/focus`, { method: "POST" });
    state.focusMac = result.focus_mac;
    renderMonitor();
  } catch (error) { alert(error.message); }
}

async function stopFocus() {
  try {
    await api("/api/focus", { method: "DELETE" });
    state.focusMac = null;
    render();
  } catch (error) { alert(error.message); }
}

function openDeviceSettings(id) {
  const device = state.dashboard.devices.find(item => item.id === id);
  if (!device) return;
  const form = $("#deviceForm");
  form.device_id.value = device.id;
  form.mac.value = device.mac_display.toUpperCase();
  form.enabled.value = String(device.enabled);
  form.name.value = device.name;
  form.location.value = device.location || "";
  const names = ["temperature_warn", "temperature_alarm", "velocity_warn", "velocity_alarm", "displacement_warn", "displacement_alarm", "frequency_warn", "frequency_alarm"];
  names.forEach(name => { form[name].value = device.thresholds?.[name] ?? ""; });
  $("#deviceDialog").showModal();
}

async function deleteCurrentDevice() {
  const form = $("#deviceForm");
  const id = Number(form.device_id.value);
  const device = state.dashboard.devices.find(item => item.id === id);
  if (!device) return;
  state.deleteDeviceId = id;
  $("#deleteMessage").textContent = `确定删除“${device.name}”吗？该设备的全部历史采集记录也会被删除，此操作无法撤销。`;
  $("#deleteDialog").showModal();
}

async function confirmDeleteDevice() {
  const id = state.deleteDeviceId;
  const device = state.dashboard.devices.find(item => item.id === id);
  if (!device) return;
  try {
    await api(`/api/devices/${id}`, { method: "DELETE" });
    state.series.delete(device.mac);
    if (state.monitorDeviceId === id) $("#monitorDialog").close();
    $("#deleteDialog").close();
    $("#deviceDialog").close();
    state.monitorDeviceId = null;
    state.deleteDeviceId = null;
    await loadDashboard();
  } catch (error) { alert(error.message); }
}

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value;
  return element.innerHTML;
}

function connectSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.socket = new WebSocket(`${protocol}://${location.host}/ws`);
  state.socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "sample") acceptSample(message.mac, message.sample, message.status);
    if (message.type === "snapshot") {
      state.focusMac = message.snapshot.focus_mac;
      if (state.dashboard) {
        state.dashboard.gateway = message.snapshot.gateway;
        state.dashboard.queue_order = message.snapshot.queue_order || [];
        for (const device of state.dashboard.devices) {
          if (message.snapshot.devices[device.mac]) device.runtime = message.snapshot.devices[device.mac];
        }
        render();
      }
      if ($("#monitorDialog").open) renderMonitor();
    }
  };
  state.socket.onclose = () => setTimeout(connectSocket, 1500);
}

document.querySelectorAll("[data-close]").forEach(button => {
  button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
});

$("#settingsButton").addEventListener("click", async () => {
  const form = $("#settingsForm");
  const values = state.dashboard.settings;
  form.gateway_driver.value = values.gateway_driver;
  form.serial_port.innerHTML = '<option value="auto">自动选择唯一 USB 串口</option>';
  try {
    const { ports } = await api("/api/serial/ports");
    for (const port of ports) {
      const option = new Option(`${port.device} · ${port.description}${port.bluetooth ? "（蓝牙虚拟串口，请勿用于网关）" : ""}`, port.device);
      option.disabled = port.bluetooth;
      form.serial_port.add(option);
    }
    $("#serialPortHint").textContent = ports.some(p => p.device === values.serial_port && p.bluetooth)
      ? "之前选择的是蓝牙虚拟串口，请改选 USB 网关或电脑蓝牙直连。"
      : "自动选口仅适用于唯一 USB 串口；多个 USB 串口时请选择实际网关。";
  } catch (error) { $("#serialPortHint").textContent = error.message; }
  if (![...form.serial_port.options].some(o => o.value === values.serial_port)) {
    form.serial_port.add(new Option(`${values.serial_port}（当前未检测到）`, values.serial_port));
  }
  form.serial_port.value = values.serial_port;
  form.baudrate.value = values.baudrate;
  form.connect_timeout_seconds.value = values.connect_timeout_seconds;
  form.max_connections.value = values.max_connections;
  form.serial_concurrency_limit.value = values.serial_concurrency_limit ?? 3;
  form.dwell_minutes.value = values.dwell_seconds / 60;
  form.persist_interval_seconds.value = values.persist_interval_seconds;
  form.web_refresh_hz.value = values.web_refresh_hz;
  form.sensor_frame_bytes.value = values.sensor_frame_bytes ?? 32;
  form.serial_connection_profile.value = values.serial_connection_profile ?? -1;
  $("#settingsDialog").showModal();
});

$("#settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/settings", { method: "PATCH", body: JSON.stringify({
      gateway_driver: form.get("gateway_driver"),
      serial_port: form.get("serial_port"),
      baudrate: Number(form.get("baudrate")),
      connect_timeout_seconds: Number(form.get("connect_timeout_seconds")),
      max_connections: Number(form.get("max_connections")),
      serial_concurrency_limit: Number(form.get("serial_concurrency_limit")),
      dwell_seconds: Number(form.get("dwell_minutes")) * 60,
      persist_interval_seconds: Number(form.get("persist_interval_seconds")),
      web_refresh_hz: Number(form.get("web_refresh_hz")),
      sensor_frame_bytes: Number(form.get("sensor_frame_bytes")),
      serial_connection_profile: Number(form.get("serial_connection_profile")),
    }) });
    $("#settingsDialog").close();
    await loadDashboard();
  } catch (error) { alert(error.message); }
});

$("#addButton").addEventListener("click", () => {
  $("#addForm").reset();
  $("#addDialog").showModal();
});

$("#addForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/devices", { method: "POST", body: JSON.stringify(Object.fromEntries(form)) });
    $("#addDialog").close();
    event.currentTarget.reset();
    await loadDashboard();
  } catch (error) { alert(error.message); }
});

$("#deviceForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const names = ["temperature_warn", "temperature_alarm", "velocity_warn", "velocity_alarm", "displacement_warn", "displacement_alarm", "frequency_warn", "frequency_alarm"];
  const thresholds = {};
  names.forEach(name => { const value = form.get(name); if (value !== "") thresholds[name] = Number(value); });
  for (const metric of ["temperature", "velocity", "displacement", "frequency"]) {
    const warn = thresholds[`${metric}_warn`];
    const alarm = thresholds[`${metric}_alarm`];
    if (warn !== undefined && alarm !== undefined && alarm < warn) {
      alert("报警值不能小于预警值");
      return;
    }
  }
  try {
    const original = state.dashboard.devices.find(device => device.id === Number(form.get("device_id")));
    const updated = await api(`/api/devices/${form.get("device_id")}`, { method: "PATCH", body: JSON.stringify({ mac: form.get("mac"), enabled: form.get("enabled") === "true", name: form.get("name"), location: form.get("location"), thresholds }) });
    if (original && original.mac !== updated.mac) {
      state.series.delete(original.mac);
      if (state.monitorDeviceId === original.id) $("#monitorDialog").close();
    }
    $("#deviceDialog").close();
    await loadDashboard();
  } catch (error) { alert(error.message); }
});

$("#deleteDeviceButton").addEventListener("click", deleteCurrentDevice);
$("#confirmDeleteButton").addEventListener("click", confirmDeleteDevice);
$("#startFocusButton").addEventListener("click", startFocus);
$("#stopFocusButton").addEventListener("click", stopFocus);
$("#backToDashboardButton").addEventListener("click", () => $("#monitorDialog").close());
$("#deviceConnectionButton").addEventListener("click", () => toggleDeviceConnection(state.monitorDeviceId));
document.querySelectorAll("[data-monitor-tab]").forEach(button => {
  button.addEventListener("click", () => switchMonitorTab(button.dataset.monitorTab));
});
document.querySelectorAll("[data-history-minutes]").forEach(button => {
  button.addEventListener("click", async () => {
    setHistoryRange(Number(button.dataset.historyMinutes));
    await loadHistoricalRange();
  });
});
$("#applyHistoryRange").addEventListener("click", async () => {
  const start = new Date($("#historyStart").value).getTime();
  const end = new Date($("#historyEnd").value).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    alert("请选择有效的开始和结束时间");
    return;
  }
  if (end - start > 7 * 24 * 60 * 60 * 1000) {
    alert("单次历史查询不能超过 7 天");
    return;
  }
  state.historyStartMs = start;
  state.historyEndMs = end;
  state.historyPage = 1;
  document.querySelectorAll("[data-history-minutes]").forEach(button => button.classList.remove("active"));
  await loadHistoricalRange();
});
$("#exportHistoryButton").addEventListener("click", () => {
  if (state.historyStartMs === null || state.historyEndMs === null) setHistoryRange(60);
  const query = historyQuery();
  window.location.href = `/api/devices/${state.monitorDeviceId}/history/export?${query}`;
});
$("#historyPrevious").addEventListener("click", async () => {
  if (state.historyPage <= 1) return;
  state.historyPage -= 1;
  await loadHistoricalRange();
});
$("#historyNext").addEventListener("click", async () => {
  if (state.historyPage * 100 >= state.historyTotal) return;
  state.historyPage += 1;
  await loadHistoricalRange();
});

let rangeTimer;
$("#rangeMinutes").addEventListener("input", event => {
  clearTimeout(rangeTimer);
  rangeTimer = setTimeout(async () => {
    const value = Math.min(10080, Math.max(1, Number(event.target.value) || 1));
    event.target.value = value;
    state.rangeMinutes = value;
    await loadHistory();
  }, 350);
});

$("#monitorDialog").addEventListener("close", () => {
  state.monitorDeviceId = null;
  state.historyRequest += 1;
});

setInterval(() => {
  const dialog = $("#monitorDialog");
  const device = state.dashboard?.devices.find(item => item.id === state.monitorDeviceId);
  if (dialog.open && device?.mac === state.focusMac && state.socket?.readyState === WebSocket.OPEN) state.socket.send("focus-heartbeat");
}, 10000);
setInterval(loadDashboard, 5000);
window.addEventListener("resize", () => { if ($("#monitorDialog").open) renderMonitor(); });
window.openMonitor = openMonitor;
window.openDeviceSettings = openDeviceSettings;
window.toggleDeviceConnection = toggleDeviceConnection;

loadDashboard().then(connectSocket).catch(error => {
  $("#gatewayBadge").textContent = `页面加载失败 · ${error.message}`;
  $("#gatewayBadge").classList.add("error");
});

$("#downloadLogsButton").addEventListener("click", () => {
  window.location.href = "/api/diagnostics/download";
});
$("#reconnectGatewayButton").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  try {
    await api("/api/gateway/reconnect", { method: "POST" });
    await loadDashboard();
    $("#settingsDialog").close();
  } catch (error) { alert(error.message); }
  finally { $("#reconnectGatewayButton").disabled = false; }
});
