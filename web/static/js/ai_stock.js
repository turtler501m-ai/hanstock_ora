(function () {
  "use strict";

  var state = { data: null, selectedId: null, view: "decisions" };
  var $ = function (selector) { return document.querySelector(selector); };

  function iconRefresh() {
    if (window.lucide) window.lucide.createIcons();
  }

  function request(path, options) {
    return fetch(path, options).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.detail || ("HTTP " + response.status));
        return body;
      });
    });
  }

  function text(value, fallback) {
    return value == null || value === "" ? (fallback || "-") : String(value);
  }

  function money(value) {
    var number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString("ko-KR") : "-";
  }

  function dateTime(value) {
    if (!value) return "-";
    var date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ko-KR");
  }

  function escapeHtml(value) {
    return text(value, "").replace(/[&<>"']/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char];
    });
  }

  function listHtml(items, empty) {
    if (!items || !items.length) return "<p>" + escapeHtml(empty) + "</p>";
    return "<ul>" + items.map(function (item) {
      return "<li>" + escapeHtml(typeof item === "object" ? JSON.stringify(item) : item) + "</li>";
    }).join("") + "</ul>";
  }

  function setBusy(busy) {
    ["#refresh-button", "#scan-button"].forEach(function (selector) {
      $(selector).disabled = busy;
    });
  }

  function showError(error) {
    var strip = $("#alert-strip");
    strip.hidden = false;
    strip.textContent = error.message || String(error);
  }

  function pipelineUrl() {
    var market = $("#market-select").value;
    var strategy = $("#strategy-select").value;
    return "/api/ai-stock/decision-pipeline?market=" + encodeURIComponent(market) +
      (strategy ? "&strategy_id=" + encodeURIComponent(strategy) : "");
  }

  function load() {
    setBusy(true);
    return request(pipelineUrl()).then(function (envelope) {
      state.data = envelope.data;
      var rows = state.data.candidates || [];
      if (!rows.some(function (row) { return row.candidate_id === state.selectedId; })) {
        state.selectedId = rows.length ? rows[0].candidate_id : null;
      }
      render();
    }).catch(showError).finally(function () { setBusy(false); iconRefresh(); });
  }

  function render() {
    renderStrategies();
    renderAlerts();
    renderSummary();
    renderList();
    renderDetail();
    var scan = state.data.latest_scan;
    $("#scan-time").textContent = scan ? dateTime(scan.completed_at || scan.started_at) : "스캔 없음";
  }

  function renderStrategies() {
    var select = $("#strategy-select");
    var selected = select.value;
    var ids = [];
    (state.data.candidates || []).forEach(function (row) {
      if (row.strategy_id && ids.indexOf(row.strategy_id) < 0) ids.push(row.strategy_id);
    });
    (state.data.recent_runs || []).forEach(function (row) {
      if (row.strategy_id && ids.indexOf(row.strategy_id) < 0) ids.push(row.strategy_id);
    });
    select.innerHTML = '<option value="">최근 실행 전략</option>' + ids.map(function (id) {
      return '<option value="' + escapeHtml(id) + '">' + escapeHtml(id) + "</option>";
    }).join("");
    if (ids.indexOf(selected) >= 0) select.value = selected;
  }

  function renderAlerts() {
    var blockers = state.data.blockers || [];
    var strip = $("#alert-strip");
    strip.hidden = !blockers.length;
    strip.textContent = blockers.join(" · ");
  }

  function renderSummary() {
    var summary = state.data.summary || {};
    var service = state.data.service || {};
    var heartbeat = service.heartbeat || {};
    var cells = [
      ["최신 후보", summary.candidate_count || 0, "분석 대상"],
      ["실제 AI 판단", summary.ai_ready_count || 0, service.ai_enabled ? service.model : "AI 비활성"],
      ["뉴스 근거", summary.news_evidence_count || 0, "종목별 연결"],
      ["위험 통과", summary.risk_pass_count || 0, "계획 생성 가능"],
      ["실행 계획", summary.planned_count || 0, "승인 전 단계"],
      ["자율 서비스", heartbeat.state || "unknown", service.approval_required ? "승인 필요" : "자동 실행"],
    ];
    $("#summary-grid").innerHTML = cells.map(function (cell, index) {
      var cls = index === 5 && cell[1] !== "running" ? " warn" : (index > 0 && index < 5 && Number(cell[1]) > 0 ? " good" : "");
      return '<div class="summary-cell' + cls + '"><span>' + escapeHtml(cell[0]) +
        "</span><strong>" + escapeHtml(cell[1]) + "</strong><em>" + escapeHtml(cell[2]) + "</em></div>";
    }).join("");
  }

  function rowsForView() {
    var rows = (state.data.candidates || []).slice();
    if (state.view === "evidence") {
      rows.sort(function (a, b) {
        return (b.evidence.narratives.length - a.evidence.narratives.length) ||
          (Number(b.narrative_score) - Number(a.narrative_score));
      });
    } else if (state.view === "risk") {
      rows.sort(function (a, b) {
        return Number(b.risk_gate.proceed) - Number(a.risk_gate.proceed);
      });
    } else if (state.view === "execution") {
      rows = rows.filter(function (row) {
        return row.workflow.plan_id || row.workflow.watch_status || row.workflow.last_run_status;
      });
    }
    return rows;
  }

  function renderList() {
    var titles = {
      decisions: "AI 판단 보드",
      evidence: "뉴스·공시 근거",
      risk: "위험 검증 결과",
      execution: "승인·주문 흐름",
      diagnostics: "실행 진단",
    };
    $("#list-title").textContent = titles[state.view];
    if (state.view === "diagnostics") {
      renderDiagnostics();
      return;
    }
    var rows = rowsForView();
    if (!rows.length) {
      $("#candidate-list").innerHTML = '<p class="empty-state">표시할 항목이 없습니다.</p>';
      return;
    }
    $("#candidate-list").innerHTML = rows.map(function (row) {
      var thesis = row.thesis;
      var workflow = row.workflow;
      var actionClass = thesis.action === "매수 검토" ? " buy" : (thesis.action === "회피" ? " avoid" : "");
      return '<button class="candidate-row' + (row.candidate_id === state.selectedId ? " active" : "") +
        '" data-id="' + row.candidate_id + '">' +
        '<span class="candidate-name"><strong>' + escapeHtml(row.name || row.symbol) + '</strong><span>' +
        escapeHtml(row.symbol) + " · " + escapeHtml(row.strategy_id) + "</span></span>" +
        '<span class="decision-action' + actionClass + '">' + escapeHtml(thesis.action) + "</span>" +
        '<span class="source-tag' + (thesis.ai_ready ? " ai" : "") + '">' + escapeHtml(thesis.source) + "</span>" +
        '<span><span class="gate-tag ' + (row.risk_gate.proceed ? "pass" : "block") + '">' +
        (row.risk_gate.proceed ? "위험 통과" : "차단") + '</span><span class="row-meta">' +
        escapeHtml(workflow.plan_status || workflow.watch_status || "") + "</span></span></button>";
    }).join("");
    document.querySelectorAll(".candidate-row").forEach(function (button) {
      button.addEventListener("click", function () {
        state.selectedId = Number(button.dataset.id);
        renderList();
        renderDetail();
      });
    });
  }

  function selected() {
    return (state.data.candidates || []).find(function (row) {
      return row.candidate_id === state.selectedId;
    });
  }

  function renderDetail() {
    if (state.view === "diagnostics") {
      renderDiagnosticDetail();
      return;
    }
    var row = selected();
    if (!row) {
      $("#decision-detail").innerHTML = '<div class="empty-detail"><p>종목을 선택하세요.</p></div>';
      return;
    }
    var thesis = row.thesis;
    var evidence = row.evidence;
    var risk = row.risk_gate;
    var flow = row.workflow;
    var riskBlocks = risk.blocked_reason || [];
    $("#decision-detail").innerHTML =
      '<div class="detail-header"><span class="section-kicker">' + escapeHtml(thesis.source) + '</span>' +
      '<div class="detail-title"><div><h2>' + escapeHtml(row.name || row.symbol) + '</h2><span class="mono-muted">' +
      escapeHtml(row.symbol) + " · " + escapeHtml(row.strategy_id) + '</span></div><div class="detail-price">' +
      money(row.current_price) + "</div></div></div>" +
      '<section class="detail-block"><h3>AI 투자논지</h3><div class="decision-action">' +
      escapeHtml(thesis.action) + "</div>" + listHtml(thesis.rationale, "투자논지가 생성되지 않았습니다.") + "</section>" +
      '<section class="detail-block"><h3>뉴스·시장 근거</h3>' +
      listHtml(evidence.narratives, "종목에 연결된 뉴스 근거가 없습니다.") +
      '<div class="detail-grid"><div class="metric"><span>시장 국면</span><strong>' + escapeHtml(evidence.market_regime) +
      '</strong></div><div class="metric"><span>데이터 품질</span><strong>' + escapeHtml(evidence.data_quality) +
      '</strong></div></div></section>' +
      '<section class="detail-block"><h3>결정적 위험 검증</h3><div class="detail-grid">' +
      metric("게이트", risk.proceed ? "통과" : "차단") + metric("위험 점수", row.risk_score) +
      metric("룰 점수", row.rule_score) + metric("AI 점수", row.ai_score) + "</div>" +
      (riskBlocks.length ? '<p class="block-reason">' + escapeHtml(riskBlocks.join(" · ")) + "</p>" : "") +
      listHtml(thesis.risks, "추가 위험요인이 기록되지 않았습니다.") + "</section>" +
      '<section class="detail-block"><h3>승인·주문 상태</h3><div class="detail-grid">' +
      metric("관찰 상태", flow.watch_status) + metric("계획", flow.plan_status || flow.plan_id) +
      metric("승인", flow.approval_status || flow.approval_id) + metric("최근 실행", flow.last_run_status) +
      "</div>" + (flow.last_blocked_reason ? '<p class="block-reason">' +
      escapeHtml(flow.last_blocked_reason) + "</p>" : "") + "</section>" +
      '<div class="detail-actions">' + actionButtons(row) + "</div>";
    bindDetailActions(row);
    iconRefresh();
  }

  function metric(label, value) {
    return '<div class="metric"><span>' + escapeHtml(label) + "</span><strong>" +
      escapeHtml(text(value)) + "</strong></div>";
  }

  function actionButtons(row) {
    var flow = row.workflow;
    var buttons = [];
    if (!flow.watch_status) {
      buttons.push('<button class="primary-command" id="watch-add"><i data-lucide="eye"></i><span>관찰 등록</span></button>');
    } else if (flow.watch_status === "discovered") {
      buttons.push('<button class="primary-command" id="watch-start"><i data-lucide="radar"></i><span>관찰 시작</span></button>');
    } else if (flow.watch_status === "watching") {
      buttons.push('<button class="primary-command" id="watch-confirm"><i data-lucide="badge-check"></i><span>투자논지 확정</span></button>');
    } else if (flow.watch_status === "confirmed" && !flow.plan_id) {
      buttons.push('<button class="primary-command" id="plan-open"><i data-lucide="file-plus-2"></i><span>실행 계획</span></button>');
    }
    if (flow.plan_id && flow.plan_status === "planned") {
      buttons.push('<button class="primary-command" id="approval-queue"><i data-lucide="send"></i><span>승인 대기열</span></button>');
    }
    return buttons.join("");
  }

  function bindDetailActions(row) {
    bind("#watch-add", function () {
      return mutate("/api/ai-stock/watchlist", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_id: row.candidate_id }),
      });
    });
    bind("#watch-start", function () { return transition(row.candidate_id, "watching"); });
    bind("#watch-confirm", function () { return transition(row.candidate_id, "confirmed"); });
    bind("#plan-open", function () { openPlan(row); });
    bind("#approval-queue", function () {
      return mutate("/api/ai-stock/execution-plans/" + row.workflow.plan_id + "/queue-approval", { method: "POST" });
    });
  }

  function bind(selector, handler) {
    var element = $(selector);
    if (element) element.addEventListener("click", function () {
      Promise.resolve(handler()).catch(showError);
    });
  }

  function transition(id, status) {
    return mutate("/api/ai-stock/watchlist/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status, reason: "AI 의사결정 운영실" }),
    });
  }

  function mutate(path, options) {
    setBusy(true);
    return request(path, options).then(load).finally(function () { setBusy(false); });
  }

  function openPlan(row) {
    $("#plan-candidate-id").value = row.candidate_id;
    $("#plan-entry").value = row.current_price || "";
    $("#plan-stop").value = row.current_price ? Math.floor(Number(row.current_price) * 0.95) : "";
    $("#plan-target").value = row.current_price ? Math.ceil(Number(row.current_price) * 1.1) : "";
    $("#plan-dialog").hidden = false;
    iconRefresh();
  }

  function renderDiagnostics() {
    var runs = state.data.recent_runs || [];
    $("#candidate-list").innerHTML = '<div class="run-list">' + (runs.length ? runs.map(function (run) {
      return '<div class="run-item"><strong>' + escapeHtml(run.strategy_id) + " · " +
        escapeHtml(run.status) + '</strong><span>' + escapeHtml(run.blocked_reason || run.run_type) +
        " · " + escapeHtml(dateTime(run.started_at)) + "</span></div>";
    }).join("") : '<p class="empty-state">실행 기록이 없습니다.</p>') + "</div>";
  }

  function renderDiagnosticDetail() {
    var service = state.data.service || {};
    var heartbeat = service.heartbeat || {};
    var orders = state.data.orders || [];
    $("#decision-detail").innerHTML =
      '<div class="detail-header"><span class="section-kicker">RUNTIME HEALTH</span><div class="detail-title"><h2>자동매매 진단</h2></div></div>' +
      '<section class="detail-block"><h3>서비스 상태</h3><div class="detail-grid">' +
      metric("AI 평가", service.ai_enabled ? "활성" : "비활성") +
      metric("자율 서비스", heartbeat.state) + metric("승인 정책", service.approval_required ? "필수" : "자동") +
      metric("모델", service.model) + '</div><p class="block-reason">' +
      escapeHtml(heartbeat.detail || "") + "</p></section>" +
      '<section class="detail-block"><h3>관리 주문</h3><div class="order-list">' +
      (orders.length ? orders.map(function (order) {
        return '<div class="run-item"><strong>#' + order.id + " " + escapeHtml(order.symbol) + " " +
          escapeHtml(order.action) + '</strong><span>' + escapeHtml(order.status) + " · " +
          escapeHtml(order.last_error || "") + "</span></div>";
      }).join("") : "<p>관리 주문이 없습니다.</p>") + "</div></section>";
  }

  document.querySelectorAll(".pipeline-tab").forEach(function (button) {
    button.addEventListener("click", function () {
      state.view = button.dataset.view;
      document.querySelectorAll(".pipeline-tab").forEach(function (tab) {
        tab.classList.toggle("active", tab === button);
      });
      renderList();
      renderDetail();
      iconRefresh();
    });
  });
  $("#refresh-button").addEventListener("click", load);
  $("#market-select").addEventListener("change", load);
  $("#strategy-select").addEventListener("change", load);
  $("#scan-button").addEventListener("click", function () {
    var market = $("#market-select").value;
    var strategy = $("#strategy-select").value || "ai_stock_default_v1";
    mutate("/api/ai-stock/scans", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market: market, strategy_id: strategy }),
    }).catch(showError);
  });
  $("#plan-close").addEventListener("click", function () { $("#plan-dialog").hidden = true; });
  $("#plan-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var payload = {
      candidate_id: Number($("#plan-candidate-id").value),
      entry_price: Number($("#plan-entry").value),
      stop_price: Number($("#plan-stop").value),
      take_profit: Number($("#plan-target").value) || null,
    };
    mutate("/api/ai-stock/execution-plans", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function () { $("#plan-dialog").hidden = true; }).catch(showError);
  });

  iconRefresh();
  load();
}());
