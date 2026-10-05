// Test console ("Try it", V1-21). Loaded as a file because the CSP forbids inline scripts.
// Rules: addEventListener only, no eval, and no HTML built from strings or from API data here.
// The Cards and Rendered views come only from POST /gui/render/{kind}, which validates the
// envelope and renders it through the server's autoescaped, sanitised templates; event rows come
// from the server-rendered event stream. The Raw view is plain text (textContent).
// The API key never reaches this page: requests ride on the session cookie, and every
// "Copy as" snippet reads the key from $RESEARCH_ENGINE_API_KEY / os.environ at run time.
"use strict";

(function () {
  const ENDPOINTS = { search: "/v1/search", fetch: "/v1/fetch", search_read: "/v1/search_read" };
  const MODELS = { search: "SearchRequest", fetch: "FetchRequest", search_read: "SearchReadRequest" };
  const RENDER_KIND = { search: "search", fetch: "document", search_read: "job" };

  // --- "Copy as" snippets: pure functions of (kind, request); exported for the unit tests ----

  // POSIX single quotes: nothing inside is special except ' itself, written as '\''.
  function shellQuote(text) {
    return "'" + text.replace(/'/g, "'\\''") + "'";
  }

  function curlSnippet(kind, request) {
    return (
      'curl -sS -X POST "$RESEARCH_ENGINE_URL' + ENDPOINTS[kind] + '" \\\n' +
      '  -H "X-API-Key: $RESEARCH_ENGINE_API_KEY" \\\n' +
      "  -H 'Content-Type: application/json' \\\n" +
      "  -d " + shellQuote(JSON.stringify(request))
    );
  }

  function pythonSnippet(kind, request) {
    const model = MODELS[kind];
    // A JSON string literal is also a valid Python string literal (same escapes).
    const literal = JSON.stringify(JSON.stringify(request));
    const call =
      kind === "search_read"
        ? [
            "        job = await c.search_read(" + model + ".model_validate_json(REQUEST_JSON))",
            "        result = await c.wait_for_job(job.id)",
          ]
        : ["        result = await c." + kind + "(" + model + ".model_validate_json(REQUEST_JSON))"];
    return [
      "import asyncio",
      "import os",
      "",
      "from research_engine_client import ResearchEngineClient",
      "from research_engine_client.models import " + model,
      "",
      "REQUEST_JSON = " + literal,
      "",
      "",
      "async def main() -> None:",
      '    url = os.environ["RESEARCH_ENGINE_URL"]',
      '    async with ResearchEngineClient(url, api_key=os.environ["RESEARCH_ENGINE_API_KEY"]) as c:',
    ]
      .concat(call)
      .concat(["        print(result.model_dump_json(indent=2))", "", "", "asyncio.run(main())", ""])
      .join("\n");
  }

  function pick(source, target, names) {
    for (const name of names) {
      if (source[name] !== undefined && source[name] !== null) {
        target[name] = source[name];
      }
    }
  }

  // The MCP tools take a flat subset of each request; fields they lack are left out.
  function mcpSnippet(kind, request) {
    const args = {};
    let tool;
    if (kind === "search") {
      tool = "web_search";
      pick(request, args, ["query", "intent", "max_results", "time_range", "language", "depth"]);
    } else if (kind === "fetch") {
      tool = "web_fetch";
      pick(request, args, ["url", "mode"]);
      args.include_html = Array.isArray(request.formats) && request.formats.indexOf("html") >= 0;
    } else {
      tool = "search_and_read";
      pick(request.search || {}, args, ["query", "intent"]);
      pick(request, args, ["top_n"]);
    }
    return JSON.stringify({ tool: tool, arguments: args }, null, 2);
  }

  function snippets(kind, request) {
    return {
      curl: curlSnippet(kind, request),
      python: pythonSnippet(kind, request),
      mcp: mcpSnippet(kind, request),
    };
  }

  if (typeof module === "object" && module && module.exports) {
    module.exports = { snippets: snippets };
    return; // loaded by node for the tests: no DOM
  }

  // --- page --------------------------------------------------------------------------------

  const STREAM = "/gui/events/stream";
  const TERMINAL = new Set(["done", "partial", "failed", "cancelled"]);
  const JOB_GIVE_UP_MS = 15 * 60 * 1000;
  const READY_WAIT_MS = 3000; // longest wait for the live stream before a sync run starts
  const STREAM_LINGER_MS = 1500; // keep the stream open briefly after a run for late events
  const MAX_TRAIL_ROWS = 2000;
  const FORMATS_WITH_HTML = ["markdown", "html"];

  const forms = {};
  document.querySelectorAll("form.run-form").forEach(function (form) {
    forms[form.dataset.kind] = form;
  });
  const demoSelect = document.getElementById("demo");
  const demoDescription = document.getElementById("demo-description");
  const runStatus = document.getElementById("run-status");
  const cancelButton = document.getElementById("cancel");
  const trail = document.getElementById("trail");
  const trailNote = document.getElementById("trail-note");
  const trailStatus = document.getElementById("trail-status");
  const runsBox = document.getElementById("runs");
  const panes = {
    cards: document.getElementById("pane-cards"),
    rendered: document.getElementById("pane-rendered"),
    raw: document.getElementById("pane-raw"),
  };
  if (!forms.search || !forms.fetch || !forms.search_read || !trail || !runsBox || !panes.raw) {
    return;
  }

  let demos = [];
  try {
    demos = JSON.parse(document.getElementById("demos").textContent || "[]");
  } catch (err) {
    demos = [];
  }

  let kind = "search";
  let active = null; // the run in flight: {controller, jobId, cancelled}

  function setStatus(text) {
    runStatus.textContent = text;
  }

  function message(text) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = text;
    return p;
  }

  // --- form <-> request ----------------------------------------------------------------------

  function setPath(obj, path, value) {
    const parts = path.split(".");
    let node = obj;
    for (let i = 0; i < parts.length - 1; i++) {
      if (typeof node[parts[i]] !== "object" || node[parts[i]] === null) {
        node[parts[i]] = {};
      }
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = value;
  }

  function getPath(obj, path) {
    let node = obj;
    for (const part of path.split(".")) {
      if (typeof node !== "object" || node === null || !(part in node)) {
        return undefined;
      }
      node = node[part];
    }
    return node;
  }

  function fields(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name;
    });
  }

  function formToRequest(form) {
    const request = {};
    for (const el of fields(form)) {
      const type = el.dataset.type || "str";
      if (type === "bool") {
        setPath(request, el.name, el.checked);
      } else if (type === "formats") {
        setPath(request, el.name, el.checked ? FORMATS_WITH_HTML.slice() : ["markdown"]);
      } else {
        const raw = el.value.trim();
        if (raw === "") {
          continue; // optional and empty: the model default applies
        }
        if (type === "int") {
          setPath(request, el.name, Number(raw));
        } else if (type === "list") {
          const items = raw.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
          if (items.length) {
            setPath(request, el.name, items);
          }
        } else {
          setPath(request, el.name, raw);
        }
      }
    }
    return request;
  }

  // Reset to the defaults, then set only the fields the request has.
  function fillForm(form, request) {
    form.reset();
    for (const el of fields(form)) {
      const value = getPath(request, el.name);
      if (value === undefined) {
        continue;
      }
      const type = el.dataset.type || "str";
      if (type === "bool") {
        el.checked = Boolean(value);
      } else if (type === "formats") {
        el.checked = Array.isArray(value) && value.indexOf("html") >= 0;
      } else if (type === "list") {
        el.value = Array.isArray(value) ? value.join(", ") : "";
      } else {
        el.value = value === null ? "" : String(value);
      }
    }
  }

  // --- tabs, snippets ------------------------------------------------------------------------

  function updateSnippets() {
    const s = snippets(kind, formToRequest(forms[kind]));
    document.getElementById("copy-curl").textContent = s.curl;
    document.getElementById("copy-python").textContent = s.python;
    document.getElementById("copy-mcp").textContent = s.mcp;
  }

  function selectKind(next) {
    if (!forms[next]) {
      return;
    }
    kind = next;
    for (const k of Object.keys(forms)) {
      forms[k].hidden = k !== next;
    }
    document.querySelectorAll(".kind-tabs [data-kind]").forEach(function (tab) {
      tab.setAttribute("aria-selected", String(tab.dataset.kind === next));
    });
    updateSnippets();
  }

  function selectView(view) {
    for (const v of Object.keys(panes)) {
      panes[v].hidden = v !== view;
    }
    document.querySelectorAll(".view-tabs [data-view]").forEach(function (tab) {
      tab.setAttribute("aria-selected", String(tab.dataset.view === view));
    });
  }

  document.querySelectorAll(".kind-tabs [data-kind]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      selectKind(tab.dataset.kind);
    });
  });
  document.querySelectorAll(".view-tabs [data-view]").forEach(function (tab) {
    tab.addEventListener("click", function () {
      selectView(tab.dataset.view);
    });
  });
  for (const form of Object.values(forms)) {
    form.addEventListener("input", updateSnippets);
    form.addEventListener("change", updateSnippets);
    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      run(form.dataset.kind);
    });
  }

  demoSelect.addEventListener("change", function () {
    const demo = demos.find(function (d) { return d.id === demoSelect.value; });
    demoDescription.textContent = demo ? demo.description : "";
    if (demo && forms[demo.kind]) {
      fillForm(forms[demo.kind], demo.request);
      selectKind(demo.kind);
    }
  });

  // Clipboard API first; where it is unavailable (plain-http origin) or refused, select the
  // text so the user can copy it by hand.
  function selectText(node) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  document.querySelectorAll("button.copy").forEach(function (button) {
    button.addEventListener("click", async function () {
      const pre = document.getElementById(button.dataset.target);
      try {
        await navigator.clipboard.writeText(pre.textContent);
        button.textContent = "Copied";
      } catch (err) {
        selectText(pre);
        button.textContent = "Selected: press Ctrl+C";
      }
      setTimeout(function () { button.textContent = "Copy"; }, 2000);
    });
  });

  // --- live event trail ----------------------------------------------------------------------

  let source = null;
  let lingerTimer = null;
  const seen = new Set();
  const parser = new DOMParser();

  function closeStream() {
    clearTimeout(lingerTimer);
    if (source) {
      source.close();
      source = null;
    }
    trailStatus.hidden = true;
  }

  // Opens the trail's only EventSource (closing any previous one). For a job: that job's events,
  // history included. Otherwise: every live event, no history; resolves once the server has
  // subscribed ("ready") so no event of the run that follows is missed (or after a timeout).
  function openStream(jobId) {
    closeStream();
    trail.replaceChildren();
    seen.clear();
    const url = jobId ? STREAM + "?job=" + encodeURIComponent(jobId) : STREAM + "?history=0";
    trailNote.textContent = jobId
      ? "Events for job " + jobId + "."
      : "All live events while the request runs (other activity on the service included).";
    const es = new EventSource(url, { withCredentials: true });
    source = es;
    es.addEventListener("log", function (msg) {
      if (source !== es) {
        return;
      }
      const id = msg.lastEventId;
      if (id) {
        if (seen.has(id)) {
          return;
        }
        seen.add(id);
      }
      // The row is server-rendered, autoescaped HTML; parsed inertly, then moved in.
      const row = parser.parseFromString(msg.data, "text/html").body.firstElementChild;
      if (!row) {
        return;
      }
      const stick = trail.scrollHeight - trail.scrollTop - trail.clientHeight <= 8;
      trail.append(document.adoptNode(row));
      while (trail.childElementCount > MAX_TRAIL_ROWS) {
        trail.firstElementChild.remove();
      }
      if (stick) {
        trail.scrollTop = trail.scrollHeight;
      }
    });
    es.addEventListener("error", function () {
      if (source === es) {
        trailStatus.hidden = false;
      }
    });
    es.addEventListener("open", function () {
      trailStatus.hidden = true;
    });
    if (jobId) {
      return Promise.resolve();
    }
    return new Promise(function (resolve) {
      const timer = setTimeout(resolve, READY_WAIT_MS);
      es.addEventListener("ready", function () {
        clearTimeout(timer);
        resolve();
      }, { once: true });
    });
  }

  function lingerThenClose() {
    clearTimeout(lingerTimer);
    lingerTimer = setTimeout(closeStream, STREAM_LINGER_MS);
  }

  // --- running -------------------------------------------------------------------------------

  function postJson(url, body, signal) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: signal,
      cache: "no-store",
    });
  }

  async function readEnvelope(response) {
    try {
      return await response.json();
    } catch (err) {
      return null;
    }
  }

  function clearResults() {
    panes.cards.replaceChildren(message("Running…"));
    panes.rendered.replaceChildren(message("Running…"));
    panes.raw.textContent = "";
  }

  // Raw: the envelope as text. Cards and Rendered: server-rendered by /gui/render/{kind}.
  async function show(renderKind, env, httpStatus) {
    panes.raw.textContent = env
      ? JSON.stringify(env, null, 2)
      : "(no JSON body; HTTP " + httpStatus + ")";
    if (!env) {
      panes.cards.replaceChildren(message("No JSON response (HTTP " + httpStatus + ")."));
      panes.rendered.replaceChildren(message("No JSON response (HTTP " + httpStatus + ")."));
      return;
    }
    let response;
    try {
      response = await postJson("/gui/render/" + renderKind, env);
    } catch (err) {
      panes.cards.replaceChildren(message("Could not render: " + err.message));
      panes.rendered.replaceChildren(message("Could not render: " + err.message));
      return;
    }
    if (!response.ok) {
      const why = response.status === 413
        ? "Too large to render here; see Raw JSON."
        : "Could not render (HTTP " + response.status + "); see Raw JSON.";
      panes.cards.replaceChildren(message(why));
      panes.rendered.replaceChildren(message(why));
      return;
    }
    const doc = parser.parseFromString(await response.text(), "text/html");
    for (const view of ["cards", "rendered"]) {
      const part = doc.querySelector('[data-view="' + view + '"]');
      const nodes = part ? Array.from(part.childNodes) : [];
      panes[view].replaceChildren.apply(
        panes[view],
        nodes.map(function (n) { return document.adoptNode(n); })
      );
    }
  }

  function setRunning(running) {
    cancelButton.hidden = !running;
    document.querySelectorAll("button.run, button.rerun").forEach(function (b) {
      b.disabled = running;
    });
  }

  function describe(env, httpStatus) {
    if (env && env.errors && env.errors.length) {
      const e = env.errors[0];
      return "HTTP " + httpStatus + ": " + e.code + ": " + e.message;
    }
    return "HTTP " + httpStatus;
  }

  async function runSync(state, runKind, request) {
    await openStream(null);
    const response = await postJson(ENDPOINTS[runKind], request, state.controller.signal);
    const env = await readEnvelope(response);
    if (response.status === 401) {
      setStatus("Your session has expired: reload the page and log in again.");
    }
    await show(RENDER_KIND[runKind], env, response.status);
    return { status: response.ok ? "done" : "failed", detail: describe(env, response.status) };
  }

  async function runJob(state, request) {
    const response = await postJson(ENDPOINTS.search_read, request, state.controller.signal);
    const submitted = await readEnvelope(response);
    if (!response.ok || !submitted || !submitted.data) {
      await show("job", submitted, response.status);
      return { status: "failed", detail: describe(submitted, response.status) };
    }
    const jobId = submitted.data.id;
    state.jobId = jobId;
    openStream(jobId);
    const deadline = Date.now() + JOB_GIVE_UP_MS;
    let progress = submitted.data.status;
    for (;;) {
      setStatus("Job " + jobId + ": " + progress + "…");
      const poll = await fetch("/v1/jobs/" + encodeURIComponent(jobId) + "?wait=30", {
        credentials: "same-origin",
        signal: state.controller.signal,
        cache: "no-store",
      });
      const env = await readEnvelope(poll);
      if (!poll.ok || !env || !env.data) {
        await show("job", env, poll.status);
        return { status: "failed", detail: describe(env, poll.status) };
      }
      const job = env.data.job;
      progress = job.status + " " + job.progress.done + "/" + job.progress.total;
      if (TERMINAL.has(job.status)) {
        await show("job", env, poll.status);
        return { status: job.status, detail: "job " + job.status };
      }
      if (Date.now() > deadline) {
        await show("job", env, poll.status);
        return { status: "timeout", detail: "gave up waiting after 15 minutes (job still " + job.status + ")" };
      }
    }
  }

  // After Cancel: cancel the job on the server, then show its final state.
  async function cancelJob(jobId) {
    try {
      await fetch("/v1/jobs/" + encodeURIComponent(jobId), {
        method: "DELETE",
        credentials: "same-origin",
        cache: "no-store",
      });
      const poll = await fetch("/v1/jobs/" + encodeURIComponent(jobId), {
        credentials: "same-origin",
        cache: "no-store",
      });
      await show("job", await readEnvelope(poll), poll.status);
    } catch (err) {
      // nothing more to show
    }
  }

  async function recordRun(record) {
    try {
      await postJson("/gui/runs", record);
    } catch (err) {
      return;
    }
    document.body.dispatchEvent(new CustomEvent("runs-changed"));
  }

  async function run(runKind) {
    const form = forms[runKind];
    if (active || !form || !form.reportValidity()) {
      return;
    }
    const request = formToRequest(form);
    const state = { controller: new AbortController(), jobId: null, cancelled: false };
    active = state;
    setRunning(true);
    clearResults();
    setStatus("Running " + runKind + "…");
    const started = performance.now();
    let outcome;
    try {
      outcome = runKind === "search_read"
        ? await runJob(state, request)
        : await runSync(state, runKind, request);
    } catch (err) {
      if (state.cancelled) {
        outcome = { status: "cancelled", detail: "cancelled" };
        if (state.jobId) {
          await cancelJob(state.jobId);
        } else {
          panes.cards.replaceChildren(message("Cancelled."));
          panes.rendered.replaceChildren(message("Cancelled."));
        }
      } else {
        outcome = { status: "failed", detail: "request failed: " + err.message };
        panes.cards.replaceChildren(message("Request failed: " + err.message));
        panes.rendered.replaceChildren(message("Request failed: " + err.message));
      }
    }
    const tookMs = Math.round(performance.now() - started);
    active = null;
    setRunning(false);
    lingerThenClose();
    if (!runStatus.textContent.startsWith("Your session")) {
      setStatus(runKind + " " + outcome.status + " in " + tookMs + " ms (" + outcome.detail + ")");
    }
    await recordRun({
      kind: runKind,
      request: request,
      job_id: state.jobId,
      status: outcome.status,
      took_ms: tookMs,
    });
  }

  cancelButton.addEventListener("click", function () {
    if (active) {
      active.cancelled = true;
      active.controller.abort();
    }
  });

  // Past runs: re-run fills the form from the stored request and submits it.
  runsBox.addEventListener("click", function (evt) {
    const button = evt.target.closest("button.rerun");
    if (!button || active) {
      return;
    }
    const runKind = button.dataset.kind;
    let request;
    try {
      request = JSON.parse(button.dataset.request);
    } catch (err) {
      return;
    }
    if (!forms[runKind]) {
      return;
    }
    demoSelect.value = "";
    demoDescription.textContent = "";
    fillForm(forms[runKind], request);
    selectKind(runKind);
    forms[runKind].requestSubmit();
  });

  window.addEventListener("pagehide", closeStream);
  selectKind("search");
})();
