/* Shared auto-refresh control. initAutoRefresh(containerId, refreshFn, storageKey)
   renders a small dropdown + pulse indicator into containerId, and calls
   refreshFn() on the selected interval. Automatically pauses while the
   browser tab is hidden (Page Visibility API) so it doesn't burn requests
   against Yahoo when nobody's even looking at the screen. */

const AUTO_REFRESH_INTERVALS = [
  { label: "Auto: Off", ms: 0 },
  { label: "Auto: 5s", ms: 5000 },
  { label: "Auto: 10s", ms: 10000 },
  { label: "Auto: 1m", ms: 60000 },
  { label: "Auto: 15m", ms: 900000 },
];
const FAST_INTERVAL_WARN_MS = 15000; // below this, warn about rate-limit risk

function initAutoRefresh(containerId, refreshFn, storageKey) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container.innerHTML = `
    <span class="ar-dot" id="${containerId}-dot"></span>
    <select id="${containerId}-select" class="ar-select" title="Automatically re-pull live prices on a timer"></select>
  `;
  const select = document.getElementById(`${containerId}-select`);
  const dot = document.getElementById(`${containerId}-dot`);
  select.innerHTML = AUTO_REFRESH_INTERVALS.map((opt, i) => `<option value="${i}">${opt.label}</option>`).join("");

  let timerId = null;
  let currentIndex = 0;

  function applyIndex(index) {
    currentIndex = index;
    localStorage.setItem(storageKey, String(index));
    select.value = String(index);
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
    const ms = AUTO_REFRESH_INTERVALS[index].ms;
    dot.classList.toggle("active", ms > 0);
    select.title = ms > 0 && ms < FAST_INTERVAL_WARN_MS
      ? "Automatically re-pull live prices on a timer. Fast intervals hit Yahoo Finance frequently — use with a smaller ticker universe (e.g. the Portfolio page) to avoid rate limits."
      : "Automatically re-pull live prices on a timer.";
    if (ms > 0) {
      timerId = setInterval(() => {
        if (!document.hidden) refreshFn();
      }, ms);
    }
  }

  select.addEventListener("change", () => applyIndex(parseInt(select.value, 10)));

  document.addEventListener("visibilitychange", () => {
    // Catch back up immediately when the tab becomes visible again, rather
    // than waiting for the next tick of a long interval (e.g. 15m).
    if (!document.hidden && AUTO_REFRESH_INTERVALS[currentIndex].ms > 0) refreshFn();
  });

  const saved = parseInt(localStorage.getItem(storageKey), 10);
  applyIndex(Number.isInteger(saved) && saved >= 0 && saved < AUTO_REFRESH_INTERVALS.length ? saved : 0);
}
