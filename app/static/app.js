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
const maxAxis = (sample, prefix) => sample ? Math.max(...["x", "y", "z"].map(axis => Math.abs(Number(sample[`${prefix}_${axis}`] ?? 0)))) : null;

function errorText(value) {
  if (!value) return "";
  const profile = value.includes(" | ") ? ` · 已尝试${value.split(" | ").slice(1).join(" | ")}` : "";
  if (value.includes("CNN_BUSY")) return `网关正忙，已自动排队重试${profile}`;
  if (value.includes("SERIAL_COLLISION_SUSPECTED")) return "连接指令期间检测到串口干扰，正在同步网关连接状态";
  if (value.includes("AT_RESPONSE_TIMEOUT")) return "网关回复不完整，正在同步网关连接状态";
  if (value.includes("AT_RESYNC_FAILED")) return "网关连接状态连续同步失败，请检查串口链路";
  if (value.includes("AT_ERROR")) return `网关拒绝指令，请下载日志查看详情${profile}`;
  if (value.includes("TIMEOUT")) return `蓝牙连接超时${profile}`;
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
    : `${gateway.name} · ${gateway.driver === "simulator" ? "模拟模式" : gateway.online ? (gateway.last_response_seconds_ago === null ? "串口已打开 · 等待网关回复" : "串口已打开 · 已收到网关回复") : "串口未打开"}`;
  badge.classList.toggle("error", Boolean(gateway.error));
  const warning = $("#gatewayWarning");
  warning.hidden = !gateway.warning_count;
  warning.textContent = gateway.warning_count
    ? `已丢弃 ${gateway.warning_count} 条损坏或不支持的数据通知。最近一次：${new Date(gateway.last_warning.time).toLocaleTimeString()}。详情见诊断日志。`
    : "";
  const connected = devices.filter(device => device.runtime.status === "connected").length;
  $("#connectedCount").textContent = connected;
  $("#connectionLimit").textContent = `/ ${gateway.maximum ?? settings.max_connections} 路连接`;
  $("#deviceCount").textContent = devices.length;
  $("#collectingCount").textContent = devices.filter(device => device.runtime.collecting).length;
  $("#queuedCount").textCo