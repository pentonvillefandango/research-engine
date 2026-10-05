// Research Engine GUI behaviour. Loaded as a file because the CSP forbids inline scripts.
// Rules: addEventListener only, no eval, and no HTML built from strings here. The server sends
// already-escaped HTML fragments and htmx swaps them in.
"use strict";

(function () {
  const STREAM = "/gui/events/stream";
  const MAX_ROWS = 2000; // rows kept in the log DOM
  const MAX_BUFFER = 2000; // rows held while paused (oldest dropped)
  const BOTTOM_SLACK_PX = 8;

  const htmx = window.htmx;
  if (!htmx) {
    return;
  }

  // The page's single live EventSource. htmx-ext-sse creates every connection (first connect,
  // its own error-retry and reconnects after htmx.process) via htmx.createEventSource, so
  // wrapping it guarantees the previous connection is always closed: never two at once.
  let current = null;
  htmx.createEventSource = function (url) {
    if (current) {
      current.close();
    }
    current = new EventSource(url, { withCredentials: true });
    return current;
  };

  function setupLog() {
    const log = document.getElementById("log");
    const form = document.getElementById("filters");
    const pauseButton = document.getElementById("pause");
    const counter = document.getElementById("buffered");
    const status = document.getElementById("sse-status");
    if (!log || !form || !pauseButton || !counter || !status) {
      return;
    }

    const seen = new Set(); // data-id of every row in the log or the pause buffer
    let buffer = []; // [{id, html}] received while paused
    let paused = false;
    let stick = true; // follow new rows only while the view is at the bottom

    function atBottom() {
      return log.scrollHeight - log.scrollTop - log.clientHeight <= BOTTOM_SLACK_PX;
    }

    function showCount() {
      counter.hidden = buffer.length === 0;
      counter.textContent = buffer.length ? buffer.length + " new (paused)" : "";
    }

    // After rows were added: cap the DOM, then follow the tail if the user was at the bottom.
    function settle() {
      while (log.childElementCount > MAX_ROWS) {
        const first = log.firstElementChild;
        seen.delete(first.getAttribute("data-id"));
        first.remove();
      }
      if (stick) {
        log.scrollTop = log.scrollHeight;
      }
    }

    log.addEventListener("scroll", function () {
      stick = atBottom();
    });

    // Fired by htmx-ext-sse before it swaps an event into the log; preventDefault skips the swap.
    // detail is the MessageEvent; its lastEventId is the event id, equal to the row's data-id.
    log.addEventListener("htmx:sseBeforeMessage", function (evt) {
      const message = evt.detail;
      const id = message.lastEventId;
      if (id) {
        if (seen.has(id)) {
          evt.preventDefault(); // duplicate (subscribe/history overlap or a reconnect replay)
          return;
        }
        seen.add(id);
      }
      if (paused) {
        evt.preventDefault();
        buffer.push({ id: id, html: message.data });
        while (buffer.length > MAX_BUFFER) {
          seen.delete(buffer.shift().id);
        }
        showCount();
      }
    });

    log.addEventListener("htmx:sseMessage", settle);

    // Connection state: an error (server gone, expired session) shows the indicator while
    // htmx-ext-sse retries; a successful (re)connect clears it.
    log.addEventListener("htmx:sseError", function () {
      status.hidden = false;
    });
    log.addEventListener("htmx:sseOpen", function () {
      status.hidden = true;
    });

    function resume() {
      paused = false;
      pauseButton.textContent = "Pause";
      pauseButton.setAttribute("aria-pressed", "false");
      if (buffer.length) {
        const html = buffer.map(function (row) { return row.html; }).join("");
        buffer = [];
        htmx.swap(log, html, { swapStyle: "beforeend", swapDelay: 0, settleDelay: 0 });
        settle();
      }
      showCount();
    }

    pauseButton.addEventListener("click", function () {
      if (paused) {
        resume();
      } else {
        paused = true;
        pauseButton.textContent = "Resume";
        pauseButton.setAttribute("aria-pressed", "true");
      }
    });

    function streamUrl() {
      const params = new URLSearchParams();
      for (const [name, value] of new FormData(form)) {
        const v = String(value).trim();
        if (v) {
          params.set(name, v);
        }
      }
      const qs = params.toString();
      return qs ? STREAM + "?" + qs : STREAM;
    }

    // New filters: close the old connection first, start an empty log (the new stream replays
    // its own history), point sse-connect at the new URL and let htmx reconnect.
    function applyFilters() {
      if (!form.checkValidity()) {
        form.reportValidity(); // never connect with a filter the server rejects (422)
        return;
      }
      const url = streamUrl();
      if (url === log.getAttribute("sse-connect")) {
        return;
      }
      if (current) {
        current.close();
        current = null;
      }
      log.replaceChildren();
      seen.clear();
      buffer = [];
      stick = true;
      showCount();
      log.setAttribute("sse-connect", url);
      htmx.process(log); // attributes changed, so htmx re-initialises the node and reconnects
    }

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      applyFilters();
    });
    form.addEventListener("change", applyFilters);
  }

  // Job detail timeline: rows rendered by the server are already in the DOM and the job-filtered
  // stream replays its history on connect, so skip any row whose id is already shown.
  function setupTimeline() {
    const timeline = document.getElementById("timeline");
    const status = document.getElementById("sse-status");
    if (!timeline || document.getElementById("log")) {
      return;
    }
    const seen = new Set();
    timeline.querySelectorAll("[data-id]").forEach(function (row) {
      seen.add(row.getAttribute("data-id"));
    });
    timeline.addEventListener("htmx:sseBeforeMessage", function (evt) {
      const id = evt.detail.lastEventId;
      if (id) {
        if (seen.has(id)) {
          evt.preventDefault();
          return;
        }
        seen.add(id);
      }
    });
    if (status) {
      timeline.addEventListener("htmx:sseError", function () {
        status.hidden = false;
      });
      timeline.addEventListener("htmx:sseOpen", function () {
        status.hidden = true;
      });
    }
  }

  setupLog();
  setupTimeline();
})();
