// AI스톡 대시보드 프런트 (§8). 점수·안전 판정을 재계산하지 않고 서버 응답을 표시만 한다.
(function () {
  "use strict";

  var state = { market: "ALL", lastCandidates: [] };

  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error((body && body.detail) || ("HTTP " + r.status));
        return body;
      });
    });
  }

  function showBanner(envelope) {
    var b = $("#ai-banner");
    var msgs = [];
    var meta = (envelope && envelope.meta) || {};
    if (meta.stale) msgs.push("⚠ 데이터가 오래되었습니다(stale)");
    if (meta.fallback_used) msgs.push("⚠ AI 실패 → 룰 기반 fallback");
    if (envelope && envelope.errors && envelope.errors.length) msgs.push("⚠ " + envelope.errors.join("; "));
    if (msgs.length) { b.textContent = msgs.join("  ·  "); b.hidden = false; }
    else { b.hidden = true; }
    var fresh = $("#s-fresh");
    if (fresh) fresh.textContent = new Date().toLocaleTimeString();
  }

  function renderSafety(s) {
    if (!s) return;
    var live = s.enable_live_trading && !s.dry_run;
    $("#s-safety").textContent = "안전모드 " + (live ? "⚠ LIVE" : "demo/dry-run") +
      " · 승인" + (s.require_approval ? "필요" : "자동");
    $("#s-safety").classList.toggle("danger", !!live);
  }

  // ---- 탭 전환 ----
  function activateTab(name) {
    document.querySelectorAll(".ai-tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll(".ai-panel").forEach(function (p) {
      p.classList.toggle("active", p.dataset.panel === name);
    });
    loadTab(name);
  }

  // ---- 각 탭 로더 ----
  function loadStatus() {
    return api("/api/ai-stock/status").then(function (env) {
      renderSafety(env.safety);
      if (env.data) {
        $("#s-model").textContent = env.data.model + " (" + env.data.model_status + ")";
      }
    }).catch(function () {});
  }

  function loadTab(name) {
    var m = state.market;
    if (name === "overview") {
      api("/api/ai-stock/overview?market=" + m).then(function (env) {
        showBanner(env);
        renderJson("#p-overview", env.data);
      }).catch(err("#p-overview"));
    } else if (name === "narratives") {
      api("/api/ai-stock/narratives?market=" + m).then(function (env) {
        showBanner(env); renderJson("#p-narratives", env.data);
      }).catch(err("#p-narratives"));
    } else if (name === "discovery") {
      loadCandidates();
    } else if (name === "watchlist") {
      api("/api/ai-stock/watchlist?market=" + m).then(function (env) {
        showBanner(env); renderWatchlist(env.data.watchlist || []);
      }).catch(err("#p-watchlist"));
    } else if (name === "portfolio") {
      api("/api/ai-stock/portfolio?market=" + m).then(function (env) {
        showBanner(env); renderPortfolio(env.data);
      }).catch(err("#p-portfolio"));
    } else if (name === "performance") {
      api("/api/ai-stock/performance?market=" + m).then(function (env) {
        showBanner(env); renderJson("#p-performance", env.data);
      }).catch(err("#p-performance"));
    } else if (name === "strategies") {
      Promise.all([
        api("/api/ai-stock/strategies?market=" + m),
        api("/api/ai-stock/automation-policies?market=" + m)
      ]).then(function (res) {
        renderStrategies(res[0].data.strategies || [], res[1].data.policies || []);
      }).catch(err("#p-strategies"));
    } else if (name === "autonomy") {
      api("/api/ai-stock/autonomy-audit?market=" + m).then(function (env) {
        showBanner(env);
        renderAutonomyAudit(env.data);
      }).catch(err("#p-autonomy"));
    } else if (name === "execution") {
      api("/api/ai-stock/timing-signals?market=" + m).then(function (env) {
        renderTiming(env.data.signals || []);
      }).catch(function () {});
      Promise.all([
        api("/api/ai-stock/execution-plans?market=" + m),
        api("/api/ai-stock/automation-runs?market=" + m)
      ]).then(function (res) {
        showBanner(res[0]);
        renderExecutionPlans(res[0].data.plans || [], res[1].data.runs || []);
      }).catch(err("#p-execution"));
    }
  }

  function loadCandidates() {
    var m = state.market;
    var minScore = $("#f-min-score").value || 0;
    var maxRiskEl = $("#f-max-risk");
    var maxRisk = maxRiskEl && maxRiskEl.value !== "" ? Number(maxRiskEl.value) : null;
    var dateEl = $("#f-date");
    var dateFilter = dateEl && dateEl.value ? dateEl.value : null;
    api("/api/ai-stock/candidates?market=" + m + "&min_score=" + minScore).then(function (env) {
      showBanner(env);
      var rows = env.data.candidates || [];
      if (maxRisk != null) {
        rows = rows.filter(function (c) { return c.risk_score == null || c.risk_score <= maxRisk; });
      }
      if (dateFilter) {
        rows = rows.filter(function (c) { return (c.data_as_of || "").slice(0, 10) === dateFilter; });
      }
      state.lastCandidates = rows;
      renderCandidates(rows);
    }).catch(err("#p-discovery"));
  }

  // ---- 렌더러 (점수 + 텍스트 상태 병행, §8.5) ----
  var DECISION_LABEL = {
    strong_watch: "강력 관찰", watch: "관찰", neutral: "중립",
    avoid: "회피", insufficient_data: "데이터 부족"
  };

  function renderCandidates(rows) {
    var root = $("#p-discovery");
    root.innerHTML = "";
    if (!rows.length) { root.appendChild(el("p", "ai-empty", "후보가 없습니다. [AI 분석 실행]을 누르세요.")); return; }
    var table = el("table", "ai-table");
    var head = el("tr");
    ["순위", "시장", "종목", "현재가", "종합", "룰", "기술", "모멘텀", "내러티브", "AI", "위험", "판단", "신선도", ""].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    table.appendChild(head);
    rows.forEach(function (c, i) {
      var tr = el("tr");
      var dec = DECISION_LABEL[c.decision] || c.decision || "-";
      [i + 1, c.market, (c.name || c.symbol), c.current_price, c.final_score,
       c.rule_score, c.technical_score, c.momentum_score, c.narrative_score,
       c.ai_score, c.risk_score].forEach(function (v) {
        tr.appendChild(el("td", null, v == null ? "-" : String(v)));
      });
      var dcell = el("td"); dcell.appendChild(el("span", "badge " + c.decision, dec)); tr.appendChild(dcell);
      tr.appendChild(el("td", null, c.fallback_used ? "fallback" : (c.data_quality || "-")));
      var btnCell = el("td");
      var b = el("button", "mini", "관찰등록");
      b.onclick = function () { registerWatch(c.candidate_id); };
      btnCell.appendChild(b); tr.appendChild(btnCell);
      tr.onclick = function (ev) { if (ev.target.tagName !== "BUTTON") showAnalysis(c.candidate_id); };
      table.appendChild(tr);
    });
    root.appendChild(table);
  }

  function renderWatchlist(rows) {
    var root = $("#p-watchlist"); root.innerHTML = "";
    if (!rows.length) { root.appendChild(el("p", "ai-empty", "관찰 후보가 없습니다.")); return; }
    var table = el("table", "ai-table");
    var head = el("tr");
    ["시장", "종목", "상태", "최초점수", "현재점수", "만료일", ""].forEach(function (h) { head.appendChild(el("th", null, h)); });
    table.appendChild(head);
    rows.forEach(function (w) {
      var tr = el("tr");
      [w.market, w.symbol, w.status, w.initial_score, w.current_score, w.expires_at || "-"].forEach(function (v) {
        tr.appendChild(el("td", null, v == null ? "-" : String(v)));
      });
      var cell = el("td");
      ["watching", "confirmed", "rejected"].forEach(function (st) {
        var b = el("button", "mini", st);
        b.onclick = function () { transitionWatch(w.candidate_id, st); };
        cell.appendChild(b);
      });
      tr.appendChild(cell); table.appendChild(tr);
    });
    root.appendChild(table);
  }

  function renderTiming(rows) {
    var root = $("#p-timing"); root.innerHTML = "";
    var h = el("h3", null, "2차 실시간 타이밍 신호");
    root.appendChild(h);
    if (!rows.length) { root.appendChild(el("p", "ai-empty", "실시간 신호 없음.")); return; }
    var table = el("table", "ai-table");
    var head = el("tr");
    ["시장", "종목", "신호", "트리거", "확신도", "처리", "기준시각"].forEach(function (x) { head.appendChild(el("th", null, x)); });
    table.appendChild(head);
    rows.forEach(function (s) {
      var tr = el("tr");
      [s.market, s.symbol, s.signal_type, s.trigger, s.ai_timing_confidence, s.decision, s.data_as_of].forEach(function (v) {
        tr.appendChild(el("td", null, v == null ? "-" : String(v)));
      });
      table.appendChild(tr);
    });
    root.appendChild(table);
  }

  var _charts = {};
  function makeChart(key, canvas, config) {
    if (_charts[key]) { _charts[key].destroy(); }
    if (typeof Chart === "undefined") return;
    _charts[key] = new Chart(canvas, config);
  }

  function showAnalysis(id) {
    activateTab("analysis");
    api("/api/ai-stock/candidates/" + id).then(function (env) {
      showBanner(env); renderAnalysis(env.data);
    }).catch(err("#p-analysis"));
  }

  function renderAnalysis(c) {
    var root = $("#p-analysis"); root.innerHTML = "";
    var dec = DECISION_LABEL[c.decision] || c.decision || "-";
    var head = el("div", "ai-analysis-head");
    head.innerHTML = "<b>" + (c.name || c.symbol) + "</b> (" + c.market + " " + c.symbol + ") · 현재가 " +
      (c.current_price == null ? "-" : c.current_price) + " · <span class='badge " + c.decision + "'>" +
      "[" + (c.final_score == null ? "-" : c.final_score) + "점 (" + dec + ")]</span> · 국면 " + (c.market_regime || "-") +
      (c.fallback_used ? " · <span style='color:#F59E0B'>fallback</span>" : "");
    root.appendChild(head);
    var canvas = el("canvas"); canvas.style.maxHeight = "320px";
    root.appendChild(canvas);
    makeChart("analysis", canvas, {
      type: "radar",
      data: {
        labels: ["룰", "기술", "모멘텀", "내러티브", "AI"],
        datasets: [{
          label: "구성 점수", data: [c.rule_score, c.technical_score, c.momentum_score, c.narrative_score, c.ai_score],
          fill: true, backgroundColor: "rgba(139,92,246,0.2)", borderColor: "#8B5CF6", pointBackgroundColor: "#8B5CF6",
        }],
      },
      options: { scales: { r: { suggestedMin: 0, suggestedMax: 100, ticks: { color: "#93a1b5" }, grid: { color: "rgba(255,255,255,0.08)" }, pointLabels: { color: "#e6edf6" } } }, plugins: { legend: { labels: { color: "#e6edf6" } } } },
    });
    var note = el("p", "ai-empty", "위험 점수 " + (c.risk_score == null ? "-" : c.risk_score) +
      " · 신뢰도(상승확률 아님) " + (c.confidence == null ? "-" : c.confidence));
    root.appendChild(note);
    var details = el("details");
    var sum = el("summary", null, "원본 데이터");
    details.appendChild(sum);
    var pre = el("pre", "ai-json"); pre.textContent = JSON.stringify(c, null, 2);
    details.appendChild(pre); root.appendChild(details);
  }

  function renderPortfolio(data) {
    var root = $("#p-portfolio"); root.innerHTML = "";
    (data.by_market || []).forEach(function (mk, idx) {
      var box = el("div", "ai-pf-block");
      box.appendChild(el("h3", null, mk.market + " (" + mk.currency + ") · 활성 후보 " + mk.active_candidates));
      var weights = mk.suggested_weights || {};
      var labels = Object.keys(weights);
      if (labels.length) {
        var canvas = el("canvas"); canvas.style.maxHeight = "260px"; box.appendChild(canvas);
        makeChart("pf-" + idx, canvas, {
          type: "doughnut",
          data: { labels: labels, datasets: [{ data: labels.map(function (k) { return weights[k]; }),
            backgroundColor: ["#8B5CF6", "#6366F1", "#10B981", "#14B8A6", "#F59E0B", "#F43F5E"] }] },
          options: { plugins: { legend: { labels: { color: "#e6edf6" } } } },
        });
      } else {
        box.appendChild(el("p", "ai-empty", "제안 비중 데이터 없음"));
      }
      root.appendChild(box);
    });
    if (data.warning) root.appendChild(el("p", "ai-empty", data.warning));
  }

  function renderStrategies(strategies, policies) {
    var root = $("#p-strategies"); root.innerHTML = "";
    var byId = {};
    policies.forEach(function (p) { byId[p.strategy_id + ":" + p.market] = p; });
    var toolbar = el("div", "ai-risk-panel");
    toolbar.appendChild(el("strong", null, "자동화 정책"));
    toolbar.appendChild(el("span", null, "Level 5=승인 대기열, Level 6=주문 실행. stale/fallback 허용은 운영 위험 플래그입니다."));
    root.appendChild(toolbar);

    var table = el("table", "ai-table");
    var head = el("tr");
    ["전략", "상태", "시장", "Level", "승인", "주문", "위험 플래그", "다음 차단"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    table.appendChild(head);

    if (!policies.length) {
      root.appendChild(el("p", "ai-empty", "자동화 정책이 없습니다. 기본값은 실행 계획까지만 자동화됩니다."));
      return;
    }

    policies.forEach(function (p) {
      var strategy = strategies.find(function (s) { return String(s.id) === String(p.strategy_id); }) || {};
      var tr = el("tr");
      tr.appendChild(el("td", null, p.strategy_id || "-"));
      tr.appendChild(el("td", null, strategy.status || "-"));
      tr.appendChild(el("td", null, p.market || "-"));
      tr.appendChild(el("td", null, String(p.automation_level == null ? "-" : p.automation_level)));
      tr.appendChild(el("td", null, p.auto_approve ? "ON" : "OFF"));
      tr.appendChild(el("td", null, p.auto_execute ? "ON" : "OFF"));
      var flags = el("td");
      (p.risk_flags || []).forEach(function (f) {
        flags.appendChild(el("span", "badge risk", f));
      });
      if (!(p.risk_flags || []).length) flags.textContent = "-";
      tr.appendChild(flags);
      tr.appendChild(el("td", null, p.next_blocked_stage || "-"));
      table.appendChild(tr);
    });
    root.appendChild(table);

    var details = el("details");
    details.appendChild(el("summary", null, "원본 정책 JSON"));
    var pre = el("pre", "ai-json");
    pre.textContent = JSON.stringify({ strategies: strategies, policies: policies }, null, 2);
    details.appendChild(pre);
    root.appendChild(details);
  }

  function renderExecutionPlans(plans, runs) {
    var root = $("#p-execution"); root.innerHTML = "";
    if (!plans.length) {
      root.appendChild(el("p", "ai-empty", "실행 계획이 없습니다. confirmed 후보만 계획으로 연결됩니다."));
      renderAutomationRuns(root, runs || []);
      return;
    }
    var table = el("table", "ai-table");
    var head = el("tr");
    ["ID", "시장", "종목", "상태", "수량", "진입", "손절", "승인", "승인DB", "갱신"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    table.appendChild(head);
    plans.forEach(function (p) {
      var tr = el("tr");
      [p.id, p.market, p.symbol, p.status, p.quantity, p.entry_price, p.stop_price,
       p.approval_status || "-", p.approval_db || "-", p.updated_at || p.created_at || "-"].forEach(function (v) {
        tr.appendChild(el("td", null, v == null ? "-" : String(v)));
      });
      table.appendChild(tr);
    });
    root.appendChild(table);
    renderAutomationRuns(root, runs || []);
    var details = el("details");
    details.appendChild(el("summary", null, "원본 실행계획 JSON"));
    var pre = el("pre", "ai-json");
    pre.textContent = JSON.stringify({ plans: plans, runs: runs || [], count: plans.length }, null, 2);
    details.appendChild(pre);
    root.appendChild(details);
  }

  function renderAutomationRuns(root, runs) {
    var wrap = el("div", "ai-run-panel");
    wrap.appendChild(el("h3", null, "Automation runs"));
    if (!runs.length) {
      wrap.appendChild(el("p", "ai-empty", "No automation run history."));
      root.appendChild(wrap);
      return;
    }
    var table = el("table", "ai-table");
    var head = el("tr");
    ["ID", "Time", "Strategy", "Market", "Type", "Level", "Status", "Blocked", "Reason"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    table.appendChild(head);
    runs.slice(0, 20).forEach(function (r) {
      var tr = el("tr");
      [r.id, r.started_at || r.created_at || "-", r.strategy_id || "-", r.market || "-",
       r.run_type || "-", r.automation_level == null ? "-" : r.automation_level,
       r.status || "-", r.blocked_stage || "-", r.blocked_reason || "-"].forEach(function (v) {
        tr.appendChild(el("td", null, v == null ? "-" : String(v)));
      });
      table.appendChild(tr);
    });
    wrap.appendChild(table);
    root.appendChild(wrap);
  }

  function renderAutonomyAudit(data) {
    var root = $("#p-autonomy"); root.innerHTML = "";
    var summary = data.summary || {};
    var mode = summary.runtime_mode || {};
    var statusClass = summary.overall_status === "complete" ? "implemented" : "partial";
    var banner = el("div", "ai-autonomy-summary");
    banner.appendChild(el("span", "autonomy-status " + statusClass, summary.judgement || "-"));
    banner.appendChild(el("strong", null, summary.reason || "-"));
    root.appendChild(banner);

    var cards = el("div", "ai-audit-grid");
    [
      ["자율 런타임", mode.autonomy_enabled ? "ON" : "OFF"],
      ["자율 환경", mode.autonomy_trading_env || "-"],
      ["승인", mode.autonomy_require_approval ? "필수" : "자동"],
      ["전역 DRY_RUN", mode.dry_run ? "true" : "false"],
      ["전역 실거래", mode.enable_live_trading ? "ON" : "OFF"],
      ["Live 하드스톱", mode.live_hard_stop_adapter || "-"]
    ].forEach(function (item) {
      var card = el("div", "ai-audit-card");
      card.appendChild(el("span", null, item[0]));
      card.appendChild(el("strong", null, String(item[1])));
      cards.appendChild(card);
    });
    root.appendChild(cards);

    var blockers = summary.blockers || [];
    if (blockers.length) {
      var block = el("div", "ai-risk-panel");
      block.appendChild(el("strong", null, "완료 판단 차단"));
      block.appendChild(el("span", null, blockers.join(" · ")));
      root.appendChild(block);
    }

    var table = el("table", "ai-table");
    var head = el("tr");
    ["K01 영역", "상태", "구현 근거", "남은 갭"].forEach(function (h) {
      head.appendChild(el("th", null, h));
    });
    table.appendChild(head);
    (data.coverage || []).forEach(function (item) {
      var tr = el("tr");
      tr.appendChild(el("td", null, item.area || item.title || "-"));
      var st = el("td");
      st.appendChild(el("span", "autonomy-status " + (item.status || "partial"), statusLabel(item.status)));
      tr.appendChild(st);
      tr.appendChild(el("td", null, item.evidence || "-"));
      tr.appendChild(el("td", null, item.gap || "-"));
      table.appendChild(tr);
    });
    root.appendChild(table);

    renderAuditCounts(root, data.status_counts || {});
    var details = el("details");
    details.appendChild(el("summary", null, "최근 자율전략 원본 레코드"));
    var pre = el("pre", "ai-json");
    pre.textContent = JSON.stringify(data.records || {}, null, 2);
    details.appendChild(pre);
    root.appendChild(details);
  }

  function statusLabel(status) {
    return { implemented: "구현", partial: "부분", blocked: "차단" }[status] || (status || "-");
  }

  function renderAuditCounts(root, counts) {
    var wrap = el("div", "ai-run-panel");
    wrap.appendChild(el("h3", null, "저장소 현황"));
    var grid = el("div", "ai-audit-grid");
    Object.keys(counts).forEach(function (name) {
      var card = el("div", "ai-audit-card");
      card.appendChild(el("span", null, name));
      card.appendChild(el("strong", null, JSON.stringify(counts[name])));
      grid.appendChild(card);
    });
    wrap.appendChild(grid);
    root.appendChild(wrap);
  }

  function renderJson(sel, data) {
    var root = $(sel); root.innerHTML = "";
    var pre = el("pre", "ai-json");
    pre.textContent = JSON.stringify(data, null, 2);
    root.appendChild(pre);
  }

  function err(sel) {
    return function (e) {
      var root = $(sel); root.innerHTML = "";
      root.appendChild(el("p", "ai-error", "오류: " + (e.message || e)));
    };
  }

  // ---- 액션 ----
  function runOneScan(market) {
    return api("/api/ai-stock/scans", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market: market, strategy_id: $("#f-strategy").value })
    });
  }

  function runScan() {
    var btn = $("#btn-scan"); btn.disabled = true; btn.textContent = "분석 중…";
    var markets = state.market === "ALL" ? ["KR", "US"] : [state.market];
    Promise.all(markets.map(runOneScan)).then(function (envs) {
      $("#s-last-scan").textContent = new Date().toLocaleTimeString();
      var summaries = markets.map(function (m, i) { return m + ": " + JSON.stringify(envs[i].data && envs[i].data.summary || {}); });
      $("#p-discovery-status").textContent = "스캔 완료 — " + summaries.join("  ·  ");
      activateTab("discovery");
    }).catch(function (e) {
      $("#p-discovery-status").textContent = "스캔 오류: " + (e.message || e);
    }).finally(function () {
      btn.disabled = false; btn.textContent = "AI 분석 실행";
    });
  }

  function registerWatch(id) {
    api("/api/ai-stock/watchlist", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_id: id })
    }).then(function () { activateTab("watchlist"); }).catch(function (e) { alert(e.message || e); });
  }

  function transitionWatch(id, status) {
    api("/api/ai-stock/watchlist/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status })
    }).then(function () { loadTab("watchlist"); }).catch(function (e) { alert(e.message || e); });
  }

  // ---- init ----
  function init() {
    document.querySelectorAll(".ai-tab").forEach(function (t) {
      t.addEventListener("click", function () { activateTab(t.dataset.tab); });
    });
    $("#f-market").addEventListener("change", function (e) {
      state.market = e.target.value;
      var active = document.querySelector(".ai-tab.active");
      if (active) loadTab(active.dataset.tab);
    });
    $("#btn-refresh").addEventListener("click", function () {
      var active = document.querySelector(".ai-tab.active");
      if (active) loadTab(active.dataset.tab);
    });
    $("#btn-scan").addEventListener("click", runScan);
    loadStatus().then(function () { activateTab("overview"); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
