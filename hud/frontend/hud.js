(function () {
  const tauri = window.__TAURI__;
  if (!tauri) return;

  const HUD_BUILD = "b3";
  const EVENT_RUNS = "ringer-runs";

  document.documentElement.classList.add("tauri-hud");
  document.title = `Ringside ${HUD_BUILD}`;

  const style = document.createElement("style");
  style.textContent = `
    .tauri-hud, .tauri-hud body {
      height: 100%;
      min-height: 100%;
      overflow: hidden;
      background: transparent;
    }
    .tauri-hud body:before { display: none; }
    .tauri-hud .shell {
      width: 100%;
      height: 100vh;
      min-height: 0;
      margin: 0;
      padding: 10px;
      overflow: hidden;
      border-radius: 14px;
      display: flex;
      flex-direction: column;
      background:
        radial-gradient(circle at 50% -20%, rgba(40,215,255,.14), transparent 24rem),
        linear-gradient(180deg, rgba(8,10,15,.94), rgba(13,17,25,.97) 60%, rgba(8,10,15,.94));
      box-shadow: 0 18px 50px rgba(0,0,0,.38);
    }
    .tauri-hud #app {
      min-height: 0;
    }
  `;
  document.head.appendChild(style);

  // Surface any frontend failure where AX can read it: the window title.
  window.addEventListener("error", event => {
    document.title = `Ringside ERR: ${event.message}`.slice(0, 120);
  });

  const currentWindow = tauri.window?.getCurrentWindow?.();
  const noDragSelector = "button, a, input, select, textarea, [data-no-drag]";

  // Swift-HUD parity: the whole background drags, while real controls stay
  // clickable even when nested inside draggable-looking chrome.
  document.addEventListener("mousedown", event => {
    if (event.button !== 0) return;
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    if (!target) return;
    if (target.closest(noDragSelector)) return;
    const drag = currentWindow?.startDragging?.();
    if (drag?.catch) drag.catch(() => {});
  });

  window.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      invoke("hide_window");
    }
  });

  listen(EVENT_RUNS, event => {
    const runs = Array.isArray(event.payload) ? event.payload : [];
    update(runs);
    renderHudTitle(runs);
  });

  function renderHudTitle(runs) {
    const liveRuns = runs.filter(run => run.state === "live");
    if (liveRuns.length > 0) {
      const agents = liveRuns.reduce((sum, run) => sum + (run.tasks || []).length, 0);
      document.title = `Ringside ${HUD_BUILD}: ${liveRuns.length} ringer${liveRuns.length === 1 ? "" : "s"} · ${agents} agent${agents === 1 ? "" : "s"}`;
    } else if (runs.length > 0) {
      const newest = newestRun(runs);
      document.title = `Ringside ${HUD_BUILD}: ${finalTickerText(newest)}`;
    } else {
      document.title = `Ringside ${HUD_BUILD}: no ringers running`;
    }
  }

  function finalTickerText(run) {
    const name = run.run_name || "ringer";
    if (run.state === "died") return `${name} · died`;
    const pass = numberOrZero(run.pass ?? run.summary?.pass ?? run.totals?.pass);
    const fail = numberOrZero(run.fail ?? run.summary?.fail ?? run.totals?.fail);
    return `${name} · ok ${pass} fail ${fail}`;
  }

  function newestRun(runs) {
    return runs.reduce((latest, run) => {
      return runTimestamp(run) > runTimestamp(latest) ? run : latest;
    }, runs[0]);
  }

  function runTimestamp(run) {
    const modified = Number(run?.mtime);
    if (Number.isFinite(modified)) return modified * 1000;
    const started = Date.parse(run?.started_at || "");
    return Number.isFinite(started) ? started : 0;
  }

  function numberOrZero(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function invoke(command) {
    return tauri.core.invoke(command);
  }

  function listen(eventName, handler) {
    return tauri.event.listen(eventName, handler);
  }
})();
