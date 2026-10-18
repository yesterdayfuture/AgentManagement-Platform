function scale(value) {
  if (value >= 100) return String(Math.round(value));
  const text = (Math.round(value * 10) / 10).toFixed(1);
  return text.endsWith(".0") ? text.slice(0, -2) : text;
}

function fixed(value, digits) {
  const text = value.toFixed(digits);
  if (!text.includes(".")) return text;
  return text.replace(/0+$/, "").replace(/\.$/, "");
}

export function formatDuration(ms) {
  const value = Math.max(0, Number(ms) || 0);
  if (value < 1000) return { value: String(Math.round(value)), unit: "毫秒" };
  if (value < 60_000) return { value: scale(value / 1000), unit: "秒" };
  if (value < 3_600_000) return { value: scale(value / 60_000), unit: "分钟" };
  return { value: scale(value / 3_600_000), unit: "小时" };
}

export function formatCount(value) {
  const number = Number(value) || 0;
  const sign = number < 0 ? "-" : "";
  const abs = Math.abs(number);
  if (abs < 10_000) return { value: sign + Math.round(abs).toLocaleString("zh-CN"), unit: "" };
  if (abs < 100_000_000) return { value: sign + scale(abs / 10_000), unit: "万" };
  return { value: sign + scale(abs / 100_000_000), unit: "亿" };
}

export function formatMoney(value) {
  const number = Number(value) || 0;
  const sign = number < 0 ? "-" : "";
  const abs = Math.abs(number);
  if (abs >= 10_000) return { value: sign + scale(abs / 10_000), unit: "万元" };
  if (abs >= 1) return { value: sign + fixed(abs, abs >= 100 ? 0 : 2), unit: "元" };
  if (abs === 0) return { value: "0", unit: "元" };
  return { value: sign + fixed(abs, abs < 0.01 ? 4 : 2), unit: "元" };
}

export function countText(value) {
  const item = formatCount(value);
  return item.unit ? `${item.value}${item.unit}` : item.value;
}

export function moneyText(value) {
  if (value === null || value === undefined || value === "") return "—";
  const item = formatMoney(value);
  return `${item.value}${item.unit}`;
}

export function durationText(ms) {
  const item = formatDuration(ms);
  return `${item.value}${item.unit}`;
}

export function prettyJson(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function axisText(value, metric) {
  if (metric === "cost") return moneyText(value);
  if (value > 0 && value < 10 && !Number.isInteger(value)) return String(Math.round(value * 10) / 10);
  return countText(value);
}
