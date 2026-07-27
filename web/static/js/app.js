// 관심종목 정렬용 상태 변수
let watchlistCache = [];
let watchlistSortKey = '';
let watchlistSortAsc = true;
let watchlistInherited = false;
let holdingsCache = [];
let holdingsSortKey = 'value';
let holdingsSortAsc = false;
let activeStrategyAuditId = '';
let schedulerPollInterval = null;

function getActiveStrategyId() {
    return document.getElementById('select-ai-ranker')?.value
        || localStorage.getItem('hanstock_ai_ranker')
        || '';
}

const formatCurrency = (value) => {
    return new Intl.NumberFormat('ko-KR', {
        style: 'currency',
        currency: 'KRW',
        maximumFractionDigits: 0
    }).format(Number(value || 0));
};

const formatPercent = (value) => {
    const numeric = Number(value || 0);
    const sign = numeric > 0 ? '+' : '';
    return `${sign}${numeric.toFixed(2)}%`;
};

const formatNumber = (value, digits = 0) => {
    const numeric = Number(value || 0);
    return numeric.toLocaleString(undefined, { maximumFractionDigits: digits });
};

const escapeHtml = (value) => {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
};

const ACTION_LABELS = {
    buy: '매수',
    sell: '매도',
    hold: '보유',
};

const STATUS_LABELS = {
    pending: '승인대기',
    executed: '처리완료',
    failed: '실패',
    rejected: '거절',
};

const toKorAction = (value) => {
    const key = String(value || 'hold').toLowerCase();
    return ACTION_LABELS[key] || value || '-';
};

const toKorStatus = (value) => {
    const key = String(value || '').toLowerCase();
    return STATUS_LABELS[key] || value || '-';
};

const ORDER_STATUS_LABELS = {
    submitted: 'Submitted',
    open: 'Open',
    partial: 'Partial',
    filled: 'Filled',
    simulated: 'Simulated',
    failed: 'Failed',
};

const orderStatusLabel = (value) => {
    const key = String(value || '').toLowerCase();
    return ORDER_STATUS_LABELS[key] || value || '-';
};

const translateReason = (value) => {
    const replacements = [
        ['stop loss', '손절 기준 도달'],
        ['take profit', '익절 기준 도달'],
        ['large profit split sell', '큰 수익 분할매도'],
        ['MACD bearish take profit', 'MACD 약세 익절'],
        ['split buy', '분할매수'],
        ['multi-strategy buy', '복합 전략 매수'],
        ['golden cross buy', '골든크로스 매수'],
        ['AI allocation target', 'AI 목표비중'],
        ['Portfolio optimizer target', '포트폴리오 목표비중'],
    ];
    let text = String(value || '-');
    replacements.forEach(([from, to]) => {
        text = text.replaceAll(from, to);
    });
    text = text.replace(/\bscore\b/g, '점수');
    text = text.replace(/\bvol\b/g, '변동성');
    return text;
};

const SKIP_REASON_LABELS = {
    'category filtered': '카테고리 제외',
    'daily loss halt blocks buy orders only': '일손실 한도 초과로 신규 매수 차단',
    'buying cash unavailable': '매수가능현금 없음',
    'capital exposure limit reached': '운용자본 한도 초과',
    'buy order exceeds buying cash': '주문금액이 매수가능현금 초과',
    'sell order pending or holding is not orderable': '매도대기/주문불가 보유',
};

const schedulerSkipReasonLabel = (row = {}) => {
    const raw = String(row.skip_reason || '').trim();
    if (raw) {
        return SKIP_REASON_LABELS[raw] || translateReason(raw);
    }
    const qty = Number(row.qty || row.signal_qty || 0);
    if (row.action === 'hold') return '유지(Hold)';
    if (qty === 0) return '주문수량 0';
    return '주문 조건 미충족';
};

const schedulerDecisionLabel = (decision, row = {}) => {
    if (decision === 'skip') return `보류: ${schedulerSkipReasonLabel(row)}`;
    return toKorDecision(decision);
};

const schedulerReasonText = (row = {}) => {
    let cleanReason = row.reason || '스케쥴 분석 결과';
    if (cleanReason.startsWith('[')) {
        const closingIdx = cleanReason.indexOf(']');
        if (closingIdx !== -1) {
            cleanReason = cleanReason.substring(closingIdx + 1).trim();
        }
    }
    if ((row.decision || (row.approval_id ? 'approved' : 'skip')) !== 'skip') {
        return translateReason(cleanReason);
    }
    return `[보류 원인: ${schedulerSkipReasonLabel(row)}] ${translateReason(cleanReason)}`;
};

const strategyReasonLabel = (reason) => {
    const text = String(reason || '').trim();
    if (!text) {
        return '데이터 부족';
    }

    const mappings = [
        ['RSI recovery', '과매도 구간에서 반등 신호가 확인됐습니다.'],
        ['RSI pullback', '단기 조정 뒤 재진입을 검토할 수 있는 구간입니다.'],
        ['MACD bullish cross', 'MACD 골든크로스가 나와 상승 전환 가능성이 있습니다.'],
        ['MACD positive', 'MACD 흐름이 플러스라 단기 모멘텀이 유지되고 있습니다.'],
        ['Bollinger rebound', '볼린저 하단 반등이 나와 기술적 되돌림 가능성이 있습니다.'],
        ['near lower band', '주가가 볼린저 하단 부근이라 반등 관찰 구간입니다.'],
        ['trend pullback', '상승 추세 안에서 눌림목이 나온 모습입니다.'],
        ['long trend pullback', '중기 상승 추세 안에서 조정이 진행 중입니다.'],
        ['20-day breakout with volume', '거래량을 동반한 20일 돌파가 나왔습니다.'],
        ['volume spike', '거래량이 평소보다 강하게 증가했습니다.'],
        ['SMA20>SMA60', '단기 이동평균이 중기선 위에 있어 추세가 우호적입니다.']
    ];

    for (const [needle, label] of mappings) {
        if (text.includes(needle)) {
            return label;
        }
    }
    return translateReason(text);
};

const aiActionGuide = (action, name) => {
    if (action === 'buy') {
        return `${name} 비중을 조금 더 실어도 된다는 판단입니다.`;
    }
    if (action === 'sell') {
        return `${name} 비중이 현재 조건 대비 다소 크므로 줄이는 편이 낫다는 판단입니다.`;
    }
    return `${name}은 지금은 비중을 크게 바꾸지 않고 유지하는 편이 낫다는 판단입니다.`;
};

const aiDecisionLabel = (action) => {
    if (action === 'buy') {
        return '비중 확대';
    }
    if (action === 'sell') {
        return '비중 축소';
    }
    return '비중 유지';
};

const aiModelStatusLabel = (status) => {
    const key = String(status || '').toLowerCase();
    const labels = {
        ready: '모델 적용',
        low_confidence: '신뢰도 낮음',
        fallback: '룰 기반',
        disabled: 'AI 꺼짐',
        queued: '룰 우선',
    };
    return labels[key] || status || '-';
};

const aiModelStatusKind = (status) => {
    const key = String(status || '').toLowerCase();
    if (key === 'ready') return 'buy';
    if (key === 'low_confidence' || key === 'fallback') return 'warn';
    if (key === 'queued') return 'hold';
    return 'hold';
};

const strategyStatusLabel = (status) => {
    const labels = {
        draft: '초안',
        verified: '검증완료',
        backtested: '백테스트완료',
        paper_running: '모의운영중',
        paper_passed: '모의운영통과',
        approved: '승인완료',
        review_required: '검토필요',
        retired: '사용중지',
    };
    return labels[String(status || '').toLowerCase()] || status || '-';
};

const strategyStatusKind = (status) => {
    const key = String(status || '').toLowerCase();
    if (key === 'approved' || key === 'paper_passed' || key === 'backtested') return 'buy';
    if (key === 'draft' || key === 'paper_running' || key === 'review_required') return 'warn';
    if (key === 'retired') return 'sell';
    return 'hold';
};

function strategyDisplayName(strategy) {
    return strategy?.display_name || strategy?.name || strategy?.id || '-';
}

function strategyOperationLabel(operation) {
    if (operation?.label) return operation.label;
    if (operation?.ready) {
        if (operation.mode === 'live') return '실전운영 가능';
        if (operation.mode === 'dry_run') return '모의주문 가능';
        return '데모운영 가능';
    }
    if (operation?.mode === 'inactive') return '선택 안됨';
    return '승인/검증 필요';
}

function buildCandidateStrategyMarkup(row) {
    const ruleScore = Number(row.rule_score ?? row.score ?? 0);
    const finalScore = Number(row.final_score ?? row.score ?? ruleScore);
    const mlScore = row.ml_score == null ? null : Number(row.ml_score);
    const modelStatus = row.ai_model_status || (row.ai_enabled ? 'fallback' : 'disabled');
    const modelVersion = row.ai_model_version || '-';
    const weight = Number(row.ai_score_weight || 0);
    const topFeatures = (row.top_features || [])
        .slice(0, 3)
        .map((item) => `<span>${escapeHtml(item.name)} ${formatNumber(item.value, 3)}</span>`)
        .join('');
    const fallback = row.ai_fallback_reason
        ? `<div class="candidate-ai-note">${escapeHtml(row.ai_fallback_reason)}</div>`
        : '';

    return `
        <div class="candidate-ai-cell">
            <div class="candidate-score-grid">
                <div><span>룰</span><strong>${formatNumber(ruleScore, 2)}</strong></div>
                <div><span>AI</span><strong>${mlScore == null ? '-' : formatNumber(mlScore, 2)}</strong></div>
                <div><span>최종</span><strong>${formatNumber(finalScore, 2)}</strong></div>
            </div>
            <div class="candidate-ai-meta">
                ${pill(aiModelStatusLabel(modelStatus), aiModelStatusKind(modelStatus))}
                <span>${escapeHtml(modelVersion)}</span>
                <span>가중 ${formatNumber(weight * 100, 0)}%</span>
            </div>
            ${topFeatures ? `<div class="candidate-feature-list">${topFeatures}</div>` : ''}
            ${fallback}
        </div>
    `;
}

function buildAiModalMarkup(payload) {
    const reasons = Array.isArray(payload.reasons) ? payload.reasons : [];
    const summary = payload.reasoning_kr || aiActionGuide(payload.action, payload.name);
    const reasonItems = reasons.length
        ? reasons.map((reason) => `<li>${escapeHtml(strategyReasonLabel(reason))}</li>`).join('')
        : '<li>뚜렷한 기술적 신호가 충분하지 않아 보수적으로 판단했습니다.</li>';

    const signalItems = [
        `AI 점수는 <strong>${escapeHtml(formatNumber(payload.score, 2))}</strong>점입니다.`,
        `현재 비중은 <strong>${escapeHtml(formatNumber(payload.currentWeight * 100, 1))}%</strong>, 목표 비중은 <strong>${escapeHtml(formatNumber(payload.targetWeight * 100, 1))}%</strong>입니다.`,
        `차이 금액은 <strong>${escapeHtml(formatCurrency(payload.deltaValue))}</strong>이며, 실행 액션은 <strong>${escapeHtml(aiDecisionLabel(payload.action))}</strong>입니다.`,
        `최근 변동성은 <strong>${escapeHtml(formatNumber(payload.volatility * 100, 1))}%</strong>로 계산되었습니다.`
    ].map((line) => `<li>${line}</li>`).join('');

    const rawReasons = reasons.length
        ? `<div class="ai-modal-raw">${escapeHtml(reasons.join(' | '))}</div>`
        : '';

    return `
        <div class="ai-modal-summary">
            <div class="ai-modal-badge ${escapeHtml(payload.action)}">${escapeHtml(aiDecisionLabel(payload.action))}</div>
            <p>${escapeHtml(summary)}</p>
        </div>
        <div class="ai-modal-section">
            <h3>한눈에 보기</h3>
            <ul class="ai-modal-list">${signalItems}</ul>
        </div>
        <div class="ai-modal-section">
            <h3>왜 이런 판단이 나왔나</h3>
            <ul class="ai-modal-list">${reasonItems}</ul>
            ${rawReasons}
        </div>
        <div class="ai-modal-section">
            <h3>읽는 법</h3>
            <p class="ai-modal-footnote">
                목표 비중은 “이 종목을 전체 자산에서 어느 정도까지 가져가면 좋은지”를 뜻합니다.
                현재 비중보다 목표 비중이 높으면 매수 쪽, 낮으면 축소 쪽으로 해석하면 됩니다.
            </p>
        </div>
    `;
}

const setTableMessage = (selector, colspan, message) => {
    const tbody = document.querySelector(selector);
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="${colspan}" class="empty-state">${escapeHtml(message)}</td></tr>`;
    }
};

const setStatus = (message, ok = false) => {
    const banner = document.getElementById('status-banner');
    if (banner) {
        banner.hidden = false;
        banner.className = `status-banner ${ok ? 'ok' : ''}`;
        banner.textContent = message;
    }
};

const setButtonBusy = (id, busy) => {
    const button = typeof id === 'string' ? document.getElementById(id) : id;
    if (button) {
        button.disabled = busy;
    }
};

const setElementText = (id, value) => {
    const element = document.getElementById(id);
    if (element) {
        element.textContent = value;
    }
    return element;
};

async function fetchJson(url, timeoutMs = 60000) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const requestUrl = new URL(url, window.location.origin);
        requestUrl.searchParams.set('_ts', Date.now().toString());
        const response = await fetch(requestUrl.toString(), {
            signal: controller.signal,
            cache: 'no-store',
            headers: {
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
            },
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.detail || `요청 실패: ${response.status}`);
        }
        return data;
    } catch (err) {
        if (err.name === 'AbortError') {
            throw new Error(`요청 시간 초과: ${url}`);
        }
        throw err;
    } finally {
        clearTimeout(timeoutId);
    }
}

async function postJson(url, payload = {}) {
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.detail || `요청 실패: ${response.status}`);
    }
    return data;
}

async function deleteJson(url) {
    const response = await fetch(url, {
        method: 'DELETE'
    });
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.detail || `요청 실패: ${response.status}`);
    }
    return data;
}

function pill(value, kind = 'hold') {
    return `<span class="pill pill-${kind}">${escapeHtml(value)}</span>`;
}

function setAiModalOpen(open) {
    const modal = document.getElementById('aiModal');
    if (!modal) {
        return;
    }
    modal.style.display = open ? 'block' : 'none';
    modal.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function setNoCandidatesModalOpen(open) {
    const modal = document.getElementById('noCandidatesModal');
    if (!modal) return;
    modal.style.display = open ? 'block' : 'none';
    modal.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function setPerformanceDetailPanelOpen(open) {
    const panel = document.getElementById('performance-detail-panel');
    if (!panel) return;
    panel.style.display = open ? 'block' : 'none';
}

function renderPerformanceDetailPanel(item) {
    const panel = document.getElementById('performance-detail-panel');
    const titleEl = document.getElementById('performanceDetailTitle');
    const subtitleEl = document.getElementById('performanceDetailSubtitle');
    const bodyEl = document.getElementById('performanceDetailBody');
    if (!panel || !titleEl || !subtitleEl || !bodyEl) return;

    const details = Array.isArray(item.details) ? item.details : [];
    titleEl.textContent = `${item.period || '-'} 성과 상세 목록`;
    subtitleEl.textContent = '선택한 성과 기간의 매수/매도 체결 기준 상세 내역입니다.';

    const pnl = Number(item.realized_pnl || 0);
    const pnlRate = Number(item.realized_pnl_rate || 0);
    const pnlClass = pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '');
    const summaryHtml = `
        <div class="performance-detail-summary">
            <div>
                <span>거래 건수</span>
                <strong>${Number(item.order_count || 0).toLocaleString()}건</strong>
            </div>
            <div>
                <span>매수/매도 금액</span>
                <strong>${formatCurrency(item.buy_amount)} / ${formatCurrency(item.sell_amount)}</strong>
            </div>
            <div>
                <span>실현손익</span>
                <strong class="${pnlClass}">${pnl > 0 ? '+' : ''}${formatCurrency(pnl)}</strong>
            </div>
            <div>
                <span>실현수익률</span>
                <strong class="${pnlClass}">${pnlRate > 0 ? '+' : ''}${pnlRate.toFixed(2)}%</strong>
            </div>
        </div>
    `;

    if (!details.length) {
        bodyEl.innerHTML = `${summaryHtml}<p class="ai-modal-footnote">해당 기간의 세부 거래가 없습니다.</p>`;
        setPerformanceDetailPanelOpen(true);
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
    }

    const rows = details.map((detail) => {
        const action = String(detail.action || '').toLowerCase();
        const pnl = Number(detail.realized_pnl || 0);
        const pnlClass = pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '');
        return `
            <tr>
                <td>${escapeHtml(detail.ts || '-')}</td>
                <td>${escapeHtml(detail.symbol || '-')}</td>
                <td>${escapeHtml(detail.name || '-')}</td>
                <td>${escapeHtml(toKorAction(action))}</td>
                <td>${Number(detail.qty || 0).toLocaleString()}</td>
                <td>${formatCurrency(detail.price)}</td>
                <td>${formatCurrency(detail.amount)}</td>
                <td class="${pnlClass}">${pnl > 0 ? '+' : ''}${formatCurrency(pnl)}</td>
                <td class="${pnlClass}">${formatPercent(detail.realized_pnl_rate || 0)}</td>
                <td>${escapeHtml(translateReason(detail.reason || '-'))}</td>
            </tr>
        `;
    }).join('');

    bodyEl.innerHTML = `
        ${summaryHtml}
        <div class="table-responsive performance-detail-table-wrap">
            <table class="performance-detail-table">
                <thead>
                    <tr>
                        <th>시간</th>
                        <th>종목</th>
                        <th>종목명</th>
                        <th>구분</th>
                        <th>수량</th>
                        <th>단가</th>
                        <th>금액</th>
                        <th>실현손익</th>
                        <th>수익률</th>
                        <th>사유</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
    setPerformanceDetailPanelOpen(true);
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function buildScanErrorModalMarkup(errorMsg) {
    return `
        <div class="ai-modal-section">
            <h3>오류 내용</h3>
            <p class="ai-modal-footnote">${escapeHtml(errorMsg)}</p>
        </div>
        <div class="ai-modal-section">
            <h3>이렇게 해보세요</h3>
            <ul class="ai-modal-list">
                <li>잠시 후 다시 <strong>찾기</strong> 버튼을 눌러보세요.</li>
                <li>인터넷 연결 상태를 확인하세요.</li>
                <li>장 시간 중(09:00~15:30)에는 데이터가 더 안정적으로 수신됩니다.</li>
                <li>문제가 계속되면 YFINANCE_TIMEOUT_SECONDS 환경변수를 늘려보세요 (기본값: 8초).</li>
            </ul>
        </div>
    `;
}

function buildNoCandidatesModalMarkup(data) {
    const summary = data.scan_summary || [];
    const minScore = data.min_score || 2;
    const scanned = data.scanned || summary.length;

    // 점수 분포
    const scoreGroups = { 0: 0, 1: 0 };
    summary.forEach(item => {
        const s = item.score || 0;
        scoreGroups[s] = (scoreGroups[s] || 0) + 1;
    });

    // 가장 높은 점수 종목들 (상위 8개)
    const top = summary.slice(0, 8);

    const scoreDistItems = Object.entries(scoreGroups)
        .sort((a, b) => Number(b[0]) - Number(a[0]))
        .map(([score, count]) => `<li><strong>${score}점</strong>: ${count}종목</li>`)
        .join('');

    // 시그널 집계: 어떤 신호가 가장 많이 발생했나
    const signalCount = {};
    summary.forEach(item => {
        (item.reasons || []).forEach(r => {
            signalCount[r] = (signalCount[r] || 0) + 1;
        });
    });
    const topSignals = Object.entries(signalCount)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 4)
        .map(([r, cnt]) => `<li>${escapeHtml(strategyReasonLabel(r))} <span class="muted">(${cnt}종목)</span></li>`)
        .join('');

    const topRows = top.map(item => {
        const scoreClass = item.score >= minScore ? 'buy' : (item.score > 0 ? 'warn' : 'sell');
        const reasonText = (item.reasons || []).map(r => strategyReasonLabel(r)).join(', ') || '신호 없음';
        const gap = minScore - item.score;
        const gapText = gap > 0 ? `<span class="muted">(${gap}점 부족)</span>` : '<span class="pill pill-buy">통과</span>';
        return `
            <tr>
                <td><span class="symbol-name">${escapeHtml(item.ticker)}</span></td>
                <td>${pill(item.score, scoreClass)} ${gapText}</td>
                <td>${formatNumber(item.rsi, 1)}</td>
                <td>${formatNumber(item.macd_hist, 1)}</td>
                <td><div class="reason-cell" title="${escapeHtml(reasonText)}">${escapeHtml(reasonText)}</div></td>
            </tr>`;
    }).join('');

    const marketMood = summary.length === 0
        ? '데이터를 수신하지 못했습니다.'
        : summary.every(i => i.score === 0)
            ? '분석한 모든 종목에서 매수 신호가 하나도 발생하지 않았습니다. 시장 전반이 관망 국면일 가능성이 높습니다.'
            : `일부 종목에서 약한 신호(${Math.max(...summary.map(i=>i.score))}점)가 있으나 기준(${minScore}점)에 미치지 못합니다. 시장 모멘텀이 아직 충분히 형성되지 않은 상태입니다.`;

    return `
        <div class="ai-modal-section">
            <h3>스캔 요약</h3>
            <ul class="ai-modal-list">
                <li>분석 종목 수: <strong>${scanned}종목</strong></li>
                <li>매수 기준 점수: <strong>${minScore}점 이상</strong></li>
                <li>매수 후보: <strong>0종목</strong></li>
            </ul>
        </div>
        <div class="ai-modal-section">
            <h3>시장 판단</h3>
            <p class="ai-modal-footnote">${escapeHtml(marketMood)}</p>
        </div>
        ${topSignals ? `
        <div class="ai-modal-section">
            <h3>감지된 부분 신호 (기준 미달)</h3>
            <ul class="ai-modal-list">${topSignals}</ul>
        </div>` : ''}
        <div class="ai-modal-section">
            <h3>점수별 종목 분포</h3>
            <ul class="ai-modal-list">${scoreDistItems || '<li>분석 데이터 없음</li>'}</ul>
        </div>
        ${topRows ? `
        <div class="ai-modal-section">
            <h3>상위 스코어 종목 상세</h3>
            <div class="table-responsive">
                <table>
                    <thead><tr><th>종목</th><th>점수</th><th>RSI</th><th>MACD</th><th>감지 신호</th></tr></thead>
                    <tbody>${topRows}</tbody>
                </table>
            </div>
        </div>` : ''}
        <div class="ai-modal-section">
            <h3>이렇게 해보세요</h3>
            <ul class="ai-modal-list">
                <li>잠시 후 다시 검색하거나, 장 시작 직후/마감 1시간 전에 시도해보세요.</li>
                <li>최소 점수를 1점으로 낮추면 더 많은 후보를 볼 수 있습니다.</li>
                <li>시장 전반이 하락 국면이라면 현금 비중을 유지하는 것이 유리합니다.</li>
            </ul>
        </div>
    `;
}

let portfolioChartInstance = null;
let periodicChartInstance = null;
let periodicActiveTab = 'daily';
let periodicDataCache = null;
let latestConfig = null;

function strategySettingFields(config) {
    return [
        { key: 'SPLIT_N', label: '분할 횟수', value: config.split_n, type: 'int', step: '1', min: '1', suffix: '회' },
        { key: 'STOP_LOSS_PCT', label: '손절 기준', value: config.stop_loss_pct, type: 'float', step: '0.1', suffix: '%' },
        { key: 'TAKE_PROFIT', label: '익절 기준', value: config.take_profit, type: 'float', step: '0.1', suffix: '%' },
        { key: 'RSI_BUY', label: 'RSI 매수선', value: config.rsi_buy, type: 'int', step: '1', min: '0', max: '100' },
        { key: 'RSI_SELL', label: 'RSI 매도선', value: config.rsi_sell, type: 'int', step: '1', min: '0', max: '100' },
        { key: 'TOTAL_CAPITAL', label: '기준 자본', value: config.total_capital, type: 'float', step: '100000', min: '0', suffix: '원' },
        { key: 'MAX_POSITIONS', label: '최대 보유종목', value: config.max_positions, type: 'int', step: '1', min: '1', suffix: '개' },
        { key: 'MAX_SINGLE_WEIGHT', label: '종목당 최대비중', value: Number(config.max_single_weight || 0) * 100, type: 'float', step: '0.1', min: '0', max: '100', suffix: '%', percent: true },
        { key: 'CASH_BUFFER', label: '현금 보유비중', value: Number(config.cash_buffer || 0) * 100, type: 'float', step: '0.1', min: '0', max: '100', suffix: '%', percent: true },
        { key: 'MAX_DAILY_LOSS_PCT', label: '일 손실 제한', value: config.max_daily_loss_pct, type: 'float', step: '0.1', min: '0', suffix: '%' },
    ];
}

function renderStrategySettingsForm(config) {
    const fields = strategySettingFields(config);
    const fieldMarkup = fields.map((field) => `
        <label class="strategy-setting-item">
            <span class="label">${escapeHtml(field.label)}</span>
            <div class="setting-input-row">
                <input
                    type="number"
                    name="${escapeHtml(field.key)}"
                    value="${escapeHtml(field.value)}"
                    step="${escapeHtml(field.step || '1')}"
                    ${field.min !== undefined ? `min="${escapeHtml(field.min)}"` : ''}
                    ${field.max !== undefined ? `max="${escapeHtml(field.max)}"` : ''}
                    data-type="${escapeHtml(field.type)}"
                    data-percent="${field.percent ? 'true' : 'false'}"
                >
                ${field.suffix ? `<span>${escapeHtml(field.suffix)}</span>` : ''}
            </div>
        </label>
    `).join('');

    return `
        <form id="strategy-settings-form" class="strategy-settings-form">
            <div class="strategy-settings-grid">${fieldMarkup}</div>
            <div class="strategy-settings-meta">
                <span class="time-muted">저장하면 즉시 현재 서버에 반영됩니다.</span>
                <button type="submit" id="btn-strategy-save">저장</button>
            </div>
        </form>
    `;
}

function renderAiStrategySummary(config) {
    const ai = config.ai_analysis || {};
    const enabled = Boolean(ai.enabled);
    const available = Boolean(ai.model_available);
    const modelStatus = enabled
        ? (available ? '모델 적용 준비' : '룰 기반 대체')
        : 'AI 꺼짐';
    const modelDetail = enabled && available
        ? `${ai.provider_label || 'OpenAI API'} / ${ai.model_type || '텍스트 모델'}`
        : (enabled ? 'OPENAI_API_KEY 없음: Seven Split 룰 점수로 분석' : 'Seven Split 룰 점수만 사용');
    const ruleWeight = Number(ai.rule_weight ?? 1) * 100;
    const scoreWeight = Number(ai.score_weight ?? 0) * 100;
    const accountText = ai.account || config.kistock_account || '-';
    const flow = ai.auto_approve ? 'AI 제안 후 자동승인 설정 켜짐' : 'AI 제안 후 승인 대기';

    setElementText('ai-summary-model', `${modelStatus} · ${ai.model_name || '-'}`);
    setElementText('ai-summary-model-detail', modelDetail);
    setElementText('ai-summary-account', accountText);
    setElementText('ai-summary-weight', `룰 ${formatNumber(ruleWeight, 0)}% / AI ${formatNumber(scoreWeight, 0)}%`);
    setElementText('ai-summary-flow', flow);

    const flowEl = document.getElementById('ai-flow-list');
    if (flowEl) {
        const items = (ai.flow || []).map((item) => `<span>${escapeHtml(item)}</span>`).join('');
        flowEl.innerHTML = items || '<span>현재 KIS 계좌와 Seven Split 전략 기준으로 후보를 분석합니다.</span>';
    }
}

async function saveStrategySettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    setButtonBusy('btn-strategy-save', true);
    try {
        const values = {};
        const inputs = Array.from(form.querySelectorAll('input[name]'));
        for (const input of inputs) {
            const raw = String(input.value || '').trim();
            if (!raw) {
                throw new Error(`${input.name} 값이 비어 있습니다.`);
            }
            let numeric = Number(raw);
            if (!Number.isFinite(numeric)) {
                throw new Error(`${input.name} 값이 숫자가 아닙니다.`);
            }
            if (input.dataset.type === 'int') {
                numeric = Math.trunc(numeric);
            }
            if (input.dataset.percent === 'true') {
                numeric = numeric / 100;
            }
            values[input.name] = String(numeric);
        }
        const result = await postJson('/api/env', { values });
        setStatus(`전략 설정을 저장했습니다. 반영 항목: ${result.updated.join(', ')}`, true);
        try {
            await renderConfig();
        } catch (e) {
            console.error("Failed to load config after save:", e);
        }
        await renderBalance();
    } catch (err) {
        setStatus(`전략 설정 저장 실패: ${err.message}`);
    } finally {
        setButtonBusy('btn-strategy-save', false);
    }
}

function renderPortfolioChart(labels, data, colors) {
    if (typeof Chart === 'undefined') {
        return;
    }

    const ctx = document.getElementById('portfolioChart').getContext('2d');
    if (portfolioChartInstance) {
        portfolioChartInstance.destroy();
    }

    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = "'Noto Sans KR', 'Inter', sans-serif";

    portfolioChartInstance = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: colors,
                borderWidth: 0,
                hoverOffset: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { boxWidth: 12, padding: 15 }
                }
            },
            cutout: '65%'
        }
    });
}

async function renderRuntime() {
    const health = await fetchJson('/api/health');
    document.getElementById('runtime-env').textContent = health.trading_env === 'real' ? '실전' : '모의';
    document.getElementById('runtime-dry-run').innerHTML = health.dry_run ? pill('차단 ON', 'warn') : pill('차단 OFF', 'buy');
    document.getElementById('runtime-order').innerHTML = health.order_submission_enabled ? pill('가능', 'buy') : pill('차단', 'warn');
    document.getElementById('runtime-real').innerHTML = health.real_orders_enabled ? pill('실주문 가능', 'sell') : pill('실주문 차단', 'hold');

    const dryRunButton = document.getElementById('btn-dry-run');
    if (dryRunButton) {
        dryRunButton.dataset.enabled = String(Boolean(health.dry_run));
        dryRunButton.textContent = health.dry_run ? '끄기' : '켜기';
    }

    const autoApprovalEnabled = Boolean(health.auto_approval_enabled);
    const autoApprovalEl = document.getElementById('runtime-auto-approval');
    const autoApprovalButton = document.getElementById('btn-auto-approval');
    if (autoApprovalEl) {
        autoApprovalEl.innerHTML = autoApprovalEnabled ? pill('켜짐', 'buy') : pill('꺼짐', 'hold');
    }
    if (autoApprovalButton) {
        autoApprovalButton.dataset.enabled = String(autoApprovalEnabled);
        autoApprovalButton.textContent = autoApprovalEnabled ? '끄기' : '켜기';
    }
        
    const tokensEl = document.getElementById('runtime-tokens');
    if (tokensEl) {
        const tokens = health.token_usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, api_calls: 0 };
        const prompt = Number(tokens.prompt_tokens || 0).toLocaleString();
        const completion = Number(tokens.completion_tokens || 0).toLocaleString();
        const total = Number(tokens.total_tokens || 0).toLocaleString();
        const calls = Number(tokens.api_calls || 0).toLocaleString();
        tokensEl.innerHTML = `${total} tkn <span style="font-size: 0.72rem; font-weight: normal; color: rgba(255,255,255,0.45); margin-left: 4px;">(P:${prompt} C:${completion}, ${calls}회)</span>`;
    }
        
    const btnSyncTrades = document.getElementById('btn-sync-trades');
    if (btnSyncTrades) {
        if (health.dry_run) {
            btnSyncTrades.disabled = true;
            btnSyncTrades.textContent = '동기화 불가 (모의 실행)';
            btnSyncTrades.title = '모의 실행(DRY_RUN) 중에는 증권사 실계좌와 동기화할 수 없습니다.';
        } else {
            btnSyncTrades.disabled = false;
            btnSyncTrades.textContent = '증권사 기록 동기화';
            btnSyncTrades.title = '';
        }
    }
}

async function toggleRuntimeOrderMode(buttonId, key, label) {
    const button = document.getElementById(buttonId);
    const nextEnabled = !(button?.dataset.enabled === 'true');
    setButtonBusy(buttonId, true);
    try {
        const result = await postJson('/api/runtime/order-mode', { key, enabled: nextEnabled });
        const stateText = nextEnabled ? '켰습니다' : '껐습니다';
        const details = `주문차단=${result.dry_run ? 'ON' : 'OFF'}, 최종 주문전송=${result.order_submission_enabled ? '가능' : '차단'}, 실전주문=${result.real_orders_enabled ? '가능' : '차단'}`;
        setStatus(`${label}을 ${stateText}. ${details}`, true);
        await Promise.all([renderRuntime(), renderConfig()]);
    } catch (err) {
        setStatus(`${label} 전환 실패: ${err.message}`);
    } finally {
        setButtonBusy(buttonId, false);
    }
}

async function toggleAutoApproval() {
    const button = document.getElementById('btn-auto-approval');
    const nextEnabled = !(button?.dataset.enabled === 'true');
    setButtonBusy('btn-auto-approval', true);
    try {
        const result = await postJson('/api/auto-approval', { enabled: nextEnabled });
        const processedCount = Number(result.processed_count || 0);
        const suffix = result.enabled && processedCount > 0 ? ` 대기 주문 ${processedCount}건을 처리했습니다.` : '';
        setStatus(`자동승인을 ${result.enabled ? '켰습니다' : '껐습니다'}.${suffix}`, true);
        await Promise.all([renderRuntime(), renderApprovals(), renderTrades(), renderBalance()]);
    } catch (err) {
        setStatus(`자동승인 전환 실패: ${err.message}`);
    } finally {
        setButtonBusy('btn-auto-approval', false);
    }
}

async function renderConfig() {
    const config = await fetchJson('/api/config');
    latestConfig = config;
    setElementText('val-account', config.kistock_account || '-');
    renderAiStrategySummary(config);
    const settingsEl = document.getElementById('settings-grid');
    settingsEl.innerHTML = renderStrategySettingsForm(config);
    const form = document.getElementById('strategy-settings-form');
    if (form) {
        form.addEventListener('submit', saveStrategySettings);
    }
}

function renderRisk(balance) {
    const holdingValue = (balance.holdings || []).reduce((sum, holding) => {
        return sum + Number(holding.value || (Number(holding.qty || 0) * Number(holding.price || 0)));
    }, 0);
    const reportedTotal = Number(balance.total_eval || 0);
    const cash = Number(balance.cash || 0);
    const exposure = Number(balance.stock_eval || holdingValue || 0);
    const total = exposure > 0 && reportedTotal < Math.max(cash, exposure)
        ? cash + exposure
        : reportedTotal;
    const cashRatio = typeof balance.cash_ratio === 'number'
        ? balance.cash_ratio
        : (total > 0 ? Math.min(1, Math.max(0, cash / total)) : 0);
    const maxPosition = Math.max(0, ...balance.holdings.map((holding) => Number(holding.value || 0)));
    const concentration = total > 0 ? Math.min(1, Math.max(0, maxPosition / total)) : 0;
    const pnl = Number(balance.pnl || 0);
    const capital = Number(latestConfig?.total_capital || total || 1);
    const lossUsage = pnl < 0 && latestConfig?.max_daily_loss_pct
        ? Math.min(999, Math.abs(pnl) / capital * 100 / latestConfig.max_daily_loss_pct * 100)
        : 0;

    setElementText('val-stock-eval', formatCurrency(exposure));
    document.getElementById('risk-cash-ratio').textContent = `${formatNumber(cashRatio * 100, 1)}%`;
    document.getElementById('risk-concentration').textContent = `${formatNumber(concentration * 100, 1)}%`;
    document.getElementById('risk-loss-usage').textContent = lossUsage > 0 ? `${formatNumber(lossUsage, 1)}% 사용` : '정상';
}

function renderHoldingAccountSummary(balance, displayTotal, realizedPnl = 0) {
    const summaryEl = document.getElementById('holding-account-summary');
    if (!summaryEl) {
        return;
    }
    const stockEval = Number(balance.stock_eval || 0);
    const cash = Number(balance.cash || 0);
    const pnl = Number(balance.pnl || 0);
    const cashRatio = typeof balance.cash_ratio === 'number'
        ? balance.cash_ratio
        : (displayTotal > 0 ? cash / displayTotal : 0);
    const stockRatio = typeof balance.stock_ratio === 'number'
        ? balance.stock_ratio
        : (displayTotal > 0 ? stockEval / displayTotal : 0);
    const count = (balance.holdings || []).length;
    const source = balance._cache?.stale
        ? `최근 저장 계좌정보 ${balance._cache.cached_at || ''}`.trim()
        : '증권앱/KIS 계좌정보';

    summaryEl.innerHTML = `
        <div>
            <span>${escapeHtml(source)}</span>
            <strong>${formatCurrency(displayTotal)}</strong>
            <small>총 평가금액</small>
        </div>
        <div>
            <span>주식 평가</span>
            <strong>${formatCurrency(stockEval)}</strong>
            <small>비중 ${formatNumber(stockRatio * 100, 1)}%</small>
        </div>
        <div>
            <span>예수금</span>
            <strong>${formatCurrency(cash)}</strong>
            <small>비중 ${formatNumber(cashRatio * 100, 1)}%</small>
        </div>
        <div>
            <span>평가손익</span>
            <strong class="${pnl >= 0 ? 'text-success' : 'text-danger'}">${formatCurrency(pnl)}</strong>
            <small>실현손익 ${formatCurrency(realizedPnl)}</small>
        </div>
        <div>
            <span>보유종목</span>
            <strong>${formatNumber(count)}개</strong>
            <small>목록 헤더 클릭 시 정렬</small>
        </div>
    `;
}

function holdingSortValue(holding, key) {
    if (key === 'name') {
        return `${holding.name || ''} ${holding.symbol || ''}`.toLowerCase();
    }
    if (key === 'symbol') {
        return String(holding.symbol || '').toLowerCase();
    }
    return Number(holding[key] || 0);
}

function sortedHoldings() {
    const rows = [...holdingsCache];
    rows.sort((a, b) => {
        const av = holdingSortValue(a, holdingsSortKey);
        const bv = holdingSortValue(b, holdingsSortKey);
        if (typeof av === 'string' || typeof bv === 'string') {
            return holdingsSortAsc
                ? String(av).localeCompare(String(bv), 'ko-KR')
                : String(bv).localeCompare(String(av), 'ko-KR');
        }
        return holdingsSortAsc ? av - bv : bv - av;
    });
    return rows;
}

function updateHoldingSortHeaders() {
    const headers = document.querySelectorAll('#table-holdings thead th');
    const headerMap = [
        { key: 'name', title: '종목' },
        { key: 'qty', title: '수량' },
        { key: 'price', title: '현재가' },
        { key: 'value', title: '평가금액' },
        { key: 'rt', title: '수익률' },
        { key: 'pnl', title: '평가손익' },
        { key: '', title: '귀속 전략' },
    ];
    headerMap.forEach((item, index) => {
        const th = headers[index];
        if (!th) {
            return;
        }
        th.dataset.sort = item.key;
        th.style.cursor = 'pointer';
        th.style.userSelect = 'none';
        th.title = `${item.title} 기준 정렬`;
        const icon = holdingsSortKey === item.key ? (holdingsSortAsc ? ' ▲' : ' ▼') : '';
        th.innerHTML = `${escapeHtml(item.title)}<span class="sort-icon">${icon}</span>`;
    });
}

function renderHoldingRows() {
    const tbodyHoldings = document.querySelector('#table-holdings tbody');
    if (!tbodyHoldings) {
        return;
    }
    tbodyHoldings.innerHTML = '';
    if (!holdingsCache.length) {
        setTableMessage('#table-holdings tbody', 8, '보유 종목이 없습니다');
        updateHoldingSortHeaders();
        return;
    }

    sortedHoldings().forEach((holding) => {
        const rtClass = Number(holding.rt || 0) >= 0 ? 'text-success' : 'text-danger';
        const qty = Number(holding.qty || 0);
        const sellableQty = Number(holding.sellable_qty ?? holding.qty ?? 0);
        const canSell = sellableQty > 0;
        const qtyText = sellableQty !== qty
            ? `${qty.toLocaleString()} <small class="time-muted">매도가능 ${sellableQty.toLocaleString()}</small>`
            : qty.toLocaleString();
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>
                <div class="symbol-name">${escapeHtml(holding.name)}</div>
                <div class="symbol-code">${escapeHtml(holding.symbol)}</div>
            </td>
            <td>${qtyText}</td>
            <td>${formatCurrency(holding.price)}</td>
            <td>${formatCurrency(holding.value || Number(holding.qty || 0) * Number(holding.price || 0))}</td>
            <td class="${rtClass}">${formatPercent(holding.rt)}</td>
            <td class="${rtClass}">${formatCurrency(holding.pnl)}</td>
            <td>${(holding.strategy_names || []).length
                ? (holding.strategy_names || []).map((name) => pill(name, 'hold')).join(' ')
                : '<span class="time-muted">귀속 미확인</span>'}</td>
            <td>
                <button type="button" class="button-ghost queue-order"
                    data-symbol="${escapeHtml(holding.symbol)}"
                    data-name="${escapeHtml(holding.name)}"
                    data-action="sell"
                    data-qty="${sellableQty}"
                    data-price="0"
                    data-reason="dashboard sell current holding"
                    data-source="dashboard_holding_sell"
                    ${canSell ? '' : 'disabled'}
                    title="${canSell ? '매도가능수량 전량 매도' : '매도가능수량 없음'}"
                    style="padding:3px 8px;font-size:0.75rem;">전량</button>
            </td>
        `;
        tbodyHoldings.appendChild(tr);
    });
    tbodyHoldings.querySelectorAll('.queue-order').forEach((button) => {
        button.addEventListener('click', () => createApprovalFromButton(button), { once: true });
    });
    updateHoldingSortHeaders();
}

function bindHoldingSortHeaders() {
    document.querySelectorAll('#table-holdings thead th').forEach((th) => {
        if (th.dataset.holdingSortBound === 'true') {
            return;
        }
        th.dataset.holdingSortBound = 'true';
        th.addEventListener('click', () => {
            const key = th.dataset.sort;
            if (!key) {
                return;
            }
            if (holdingsSortKey === key) {
                holdingsSortAsc = !holdingsSortAsc;
            } else {
                holdingsSortKey = key;
                holdingsSortAsc = key === 'name' || key === 'symbol';
            }
            renderHoldingRows();
        });
    });
    updateHoldingSortHeaders();
}

async function renderBalance() {
    try {
        const [balance, perf] = await Promise.all([
            fetchJson('/api/balance', 30000),
            fetchJson('/api/performance').catch(() => ({ realized_pnl: 0 }))
        ]);
        const holdingValue = (balance.holdings || []).reduce((sum, holding) => {
            return sum + Number(holding.value || (Number(holding.qty || 0) * Number(holding.price || 0)));
        }, 0);
        const displayTotal = holdingValue > 0 && Number(balance.total_eval || 0) < Math.max(Number(balance.cash || 0), holdingValue)
            ? Number(balance.cash || 0) + holdingValue
            : Number(balance.total_eval || 0);

        const principal = Number(latestConfig?.account_initial_capital || latestConfig?.total_capital || 0);
        const accountPnl = displayTotal - principal;
        const accountReturnRate = principal > 0 ? (accountPnl / principal) * 100 : 0;
        const evalPnl = Number(balance.pnl || 0);
        const evalCost = Math.max(0, Number(balance.stock_eval || holdingValue || 0) - evalPnl);
        const returnRate = evalCost > 0 ? (evalPnl / evalCost) * 100 : 0;
        const realizedPnl = Number(perf.realized_pnl || 0);

        renderTotalPnlBreakdown({
            principal,
            displayTotal,
            accountPnl,
            realizedPnl,
            evalPnl,
            recordStartedAt: perf.record_started_at || '',
            holdings: balance.holdings || []
        });

        setElementText('val-total', formatCurrency(displayTotal));
        setElementText('val-principal', formatCurrency(principal));
        const accountPnlEl = setElementText('val-account-pnl', formatCurrency(accountPnl));
        if (accountPnlEl) {
            accountPnlEl.className = `value ${accountPnl >= 0 ? 'text-success' : 'text-danger'}`;
        }
        const accountReturnEl = setElementText('val-account-return', formatPercent(accountReturnRate));
        if (accountReturnEl) {
            accountReturnEl.className = `value ${accountReturnRate >= 0 ? 'text-success' : 'text-danger'}`;
        }
        setElementText('val-cash', formatCurrency(balance.cash));
        setElementText('val-realized', formatCurrency(realizedPnl));
        const realizedEl = document.getElementById('val-realized');
        if (realizedEl) {
            realizedEl.className = `value ${realizedPnl >= 0 ? 'text-success' : 'text-danger'}`;
        }
        const returnEl = setElementText('val-return', formatPercent(returnRate));
        if (returnEl) {
            returnEl.className = `value ${returnRate >= 0 ? 'text-success' : 'text-danger'}`;
        }

        const pnlEl = document.getElementById('val-pnl');
        pnlEl.textContent = formatCurrency(evalPnl);
        pnlEl.className = `value ${evalPnl >= 0 ? 'text-success' : 'text-danger'}`;

        const tbodyHoldings = document.querySelector('#table-holdings tbody');
        tbodyHoldings.innerHTML = '';

        const chartLabels = ['현금'];
        const chartData = [balance.cash];
        const chartColors = ['rgba(148, 163, 184, 0.7)'];
        const colors = [
            'rgba(59, 130, 246, 0.7)',
            'rgba(16, 185, 129, 0.7)',
            'rgba(139, 92, 246, 0.7)',
            'rgba(245, 158, 11, 0.7)',
            'rgba(236, 72, 153, 0.7)',
            'rgba(14, 165, 233, 0.7)'
        ];

        if (!balance.holdings.length) {
            setTableMessage('#table-holdings tbody', 8, '보유 종목이 없습니다');
        }

        balance.holdings.forEach((holding, idx) => {
            const rtClass = holding.rt >= 0 ? 'text-success' : 'text-danger';
            const qty = Number(holding.qty || 0);
            const sellableQty = Number(holding.sellable_qty ?? holding.qty ?? 0);
            const canSell = sellableQty > 0;
            const qtyText = sellableQty !== qty
                ? `${qty.toLocaleString()} <small class="time-muted">매도가능 ${sellableQty.toLocaleString()}</small>`
                : qty.toLocaleString();
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <div class="symbol-name">${escapeHtml(holding.name)}</div>
                    <div class="symbol-code">${escapeHtml(holding.symbol)}</div>
                </td>
                <td>${qtyText}</td>
                <td>${formatCurrency(holding.price)}</td>
                <td>${formatCurrency(holding.value || Number(holding.qty || 0) * Number(holding.price || 0))}</td>
                <td class="${rtClass}">${formatPercent(holding.rt)}</td>
                <td class="${rtClass}">${formatCurrency(holding.pnl)}</td>
                <td>
                    <button type="button" class="button-ghost queue-order"
                        data-symbol="${escapeHtml(holding.symbol)}"
                        data-name="${escapeHtml(holding.name)}"
                        data-action="sell"
                        data-qty="${sellableQty}"
                        data-price="0"
                        data-reason="dashboard sell current holding"
                        data-source="dashboard_holding_sell"
                        ${canSell ? '' : 'disabled'}
                        title="${canSell ? '매도가능수량 전량 매도' : '매도가능수량 없음'}"
                        style="padding:3px 8px;font-size:0.75rem;">전량</button>
                </td>
            `;
            tbodyHoldings.appendChild(tr);

            chartLabels.push(holding.name || holding.symbol);
            chartData.push(holding.value || holding.qty * holding.price);
            chartColors.push(colors[idx % colors.length]);
        });
        tbodyHoldings.querySelectorAll('.queue-order').forEach((button) => {
            button.addEventListener('click', () => createApprovalFromButton(button), { once: true });
        });
        holdingsCache = (balance.holdings || []).map((holding) => ({ ...holding }));
        renderHoldingAccountSummary(balance, displayTotal, realizedPnl);
        bindHoldingSortHeaders();
        renderHoldingRows();

        renderPortfolioChart(chartLabels, chartData, chartColors);
        renderRisk(balance);
        document.getElementById('last-updated').textContent = `마지막 갱신 ${new Date().toLocaleTimeString('ko-KR')}`;
        if (balance._cache?.stale) {
            setStatus(`KIS 계좌 API가 일시 실패해 최근 정상 데이터(${balance._cache.cached_at || '저장됨'})를 표시합니다.`);
        } else {
            setStatus('대시보드 연결 완료. 계좌 정보를 불러왔습니다.', true);
        }
    } catch (err) {
        console.error('Failed to fetch balance data', err);
        setElementText('val-total', '불러오기 실패');
        setElementText('val-principal', '불러오기 실패');
        setElementText('val-account-pnl', '불러오기 실패');
        setElementText('val-account-return', '-');
        setElementText('val-cash', '불러오기 실패');
        setElementText('val-pnl', '불러오기 실패');
        setElementText('val-return', '-');
        setStatus(`계좌 API 오류: ${err.message}`);
        setTableMessage('#table-holdings tbody', 8, err.message);
    }
}

function renderTotalPnlBreakdown({ principal, displayTotal, accountPnl, realizedPnl, evalPnl, recordStartedAt, holdings }) {
    const panel = document.getElementById('total-pnl-breakdown');
    const tbody = document.querySelector('#table-total-pnl-breakdown tbody');
    const card = document.getElementById('total-pnl-card');
    const closeButton = document.getElementById('btn-close-total-pnl');
    if (!panel || !tbody || !card) {
        return;
    }

    const otherChange = accountPnl - realizedPnl - evalPnl;
    const rows = [
        ['계좌 전체', '초기자산 대비 총손익', accountPnl, `${formatCurrency(principal)} → ${formatCurrency(displayTotal)}`],
        ['확정 손익', '기록 이후 실현손익', realizedPnl, `${recordStartedAt ? recordStartedAt.slice(0, 10) + '부터 ' : ''}KIS 체결기록으로 계산`],
        ['보유 손익', '현재 평가손익', evalPnl, '현재 보유 종목의 증권사 평가손익 합계'],
        ['기준 조정', '기록 시작 이전·미집계 누적손익', otherChange, '과거 매입원가 누락분과 수수료·세금·입출금 포함 가능'],
    ];
    (holdings || []).forEach((holding) => {
        rows.push([
            '보유 종목',
            `${holding.name || holding.symbol || '-'} (${holding.symbol || '-'})`,
            Number(holding.pnl || 0),
            `평가금 ${formatCurrency(holding.value || 0)} · 수익률 ${formatPercent(holding.rt || 0)}`,
        ]);
    });

    tbody.innerHTML = rows.map(([category, item, amount, description]) => {
        const value = Number(amount || 0);
        const valueClass = value > 0 ? 'text-success' : (value < 0 ? 'text-danger' : '');
        return `
            <tr>
                <td>${escapeHtml(category)}</td>
                <td>${escapeHtml(item)}</td>
                <td class="${valueClass}">${value > 0 ? '+' : ''}${formatCurrency(value)}</td>
                <td>${escapeHtml(description)}</td>
            </tr>
        `;
    }).join('');

    const setExpanded = (expanded) => {
        panel.hidden = !expanded;
        card.setAttribute('aria-expanded', String(expanded));
        if (expanded) {
            panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    };
    if (card.dataset.breakdownBound !== 'true') {
        card.dataset.breakdownBound = 'true';
        card.addEventListener('click', () => setExpanded(panel.hidden));
        card.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                setExpanded(panel.hidden);
            }
        });
        closeButton?.addEventListener('click', () => setExpanded(false));
    }
}

async function renderOptimizer() {
    setButtonBusy('btn-optimizer', true);
    setTableMessage('#table-optimizer tbody', 7, '포트폴리오 최적 비중을 계산하고 있습니다...');
    try {
        const data = await fetchJson('/api/portfolio-optimizer');
        const tbody = document.querySelector('#table-optimizer tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!data.positions.length) {
            setTableMessage('#table-optimizer tbody', 7, '계산할 보유 종목이 없습니다');
            return;
        }

        data.positions.forEach((row) => {
            const action = String(row.rebalance_action || 'hold').toLowerCase();
            const kind = action === 'buy' ? 'buy' : (action === 'sell' ? 'sell' : 'hold');
            const reason = `포트폴리오 목표비중 ${formatNumber(row.target_weight * 100, 1)}%; 점수=${formatNumber(row.score, 1)}, 변동성=${formatNumber(row.volatility * 100, 1)}%`;
            const queueButton = action === 'hold'
                ? `<button type="button" class="button-ghost" disabled title="비중 유지 상태이므로 주문할 내역이 없습니다." style="opacity:0.3; cursor:not-allowed;">변경없음</button>`
                : `<button type="button" class="button-ghost queue-order"
                    data-symbol="${escapeHtml(row.symbol)}"
                    data-name="${escapeHtml(row.name)}"
                    data-action="${escapeHtml(action)}"
                    data-qty="${Number(row.rebalance_qty || 0)}"
                    data-price="${Number(row.price || 0)}"
                    data-reason="${escapeHtml(reason)}"
                    data-source="portfolio-optimizer">승인대기</button>`;
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <div class="symbol-name">${escapeHtml(row.name)}</div>
                    <div class="symbol-code">${escapeHtml(row.symbol)}</div>
                </td>
                <td>${pill(formatNumber(row.score, 1), Number(row.score || 0) >= 3 ? 'buy' : 'hold')}</td>
                <td>${formatNumber(row.volatility * 100, 1)}%</td>
                <td>${formatNumber(row.current_weight * 100, 1)}%</td>
                <td>${formatNumber(row.target_weight * 100, 1)}%</td>
                <td>${pill(toKorAction(action), kind)}</td>
                <td>${queueButton}</td>
            `;
            tbody.appendChild(tr);
        });
        bindQueueButtons();
        const hasOrders = data.positions.some(row => String(row.rebalance_action || 'hold').toLowerCase() !== 'hold');
        const batchBtn = document.getElementById('btn-optimizer-batch');
        if (batchBtn) {
            batchBtn.style.display = hasOrders ? 'inline-block' : 'none';
        }
    } catch (err) {
        setTableMessage('#table-optimizer tbody', 7, err.message);
        const batchBtn = document.getElementById('btn-optimizer-batch');
        if (batchBtn) {
            batchBtn.style.display = 'none';
        }
    } finally {
        setButtonBusy('btn-optimizer', false);
    }
}

async function syncStrategiesToDropdown() {
    try {
        const data = await fetchJson('/api/ai-strategies');
        const select = document.getElementById('select-ai-ranker');
        if (!select) return;

        select.innerHTML = '';
        const strategies = (data.strategies || []).filter((strategy) => strategy.status !== 'retired');
        const uniqueStrategies = [];

        if (strategies.length === 0) {
            const opt = document.createElement('option');
            opt.value = 'rule_only_default';
            opt.textContent = '⚙️ 기본 기술 룰베이스 랭커';
            select.appendChild(opt);
        } else {
            // Group by strategy name to avoid duplicates in the dropdown
            const grouped = {};
            strategies.forEach((strategy) => {
                const name = strategy.name;
                if (!grouped[name]) {
                    grouped[name] = [];
                }
                grouped[name].push(strategy);
            });

            Object.keys(grouped).forEach((name) => {
                const group = grouped[name];
                // Sort to pick the best representative: selected first, then highest version, then alphabetical/id descending
                group.sort((a, b) => {
                    if (a.selected && !b.selected) return -1;
                    if (!a.selected && b.selected) return 1;
                    const aVer = a.strategy_version || 1;
                    const bVer = b.strategy_version || 1;
                    if (aVer !== bVer) return bVer - aVer;
                    return b.id.localeCompare(a.id);
                });
                uniqueStrategies.push(group[0]);
            });

            // Sort uniqueStrategies so selected is first, then name alphabetical
            uniqueStrategies.sort((a, b) => {
                if (a.selected && !b.selected) return -1;
                if (!a.selected && b.selected) return 1;
                return a.name.localeCompare(b.name);
            });

            uniqueStrategies.forEach((strategy) => {
                const opt = document.createElement('option');
                opt.value = strategy.id;
                opt.textContent = `${strategy.selected ? '* ' : ''}${strategy.name} · ${strategyStatusLabel(strategy.status)} · v${strategy.strategy_version || 1}`;
                select.appendChild(opt);
            });
        }

        const active = uniqueStrategies.find((strategy) => strategy.selected);
        if (active) {
            select.value = active.id;
            localStorage.setItem('hanstock_ai_ranker', active.id);
        } else if (strategies.length > 0) {
            const sharedOption = document.createElement('option');
            sharedOption.value = '';
            sharedOption.textContent = '공용 관심종목 (선택된 전략 없음)';
            select.insertBefore(sharedOption, select.firstChild);
            select.value = '';
            localStorage.removeItem('hanstock_ai_ranker');
        } else if (select.options.length > 0) {
            select.value = select.options[0].value;
            localStorage.setItem('hanstock_ai_ranker', select.value);
        }
    } catch (err) {
        console.error('Failed to sync strategies to dropdown:', err);
    }
}

async function renderStrategyContext() {
    try {
        const data = await fetchJson('/api/strategy-context');
        const active = data.active_strategy || {};
        const safety = data.safety || {};
        const gate = active.approval_gate || {};
        const operation = active.operation_status || {};
        setElementText('strategy-context-name', active.name || '-');
        setElementText('strategy-context-detail', `${active.model || '-'} · AI ${formatNumber(Number(active.ai_weight || 0) * 100, 0)}%`);
        setElementText('strategy-context-status', strategyStatusLabel(active.status));
        setElementText('strategy-context-version', active.strategy_version ? `v${active.strategy_version}` : '-');
        setElementText('strategy-context-safety', `${safety.trading_env || '-'} / ${safety.dry_run ? 'DRY_RUN' : 'LIVE'}`);
        setElementText('strategy-context-approval', strategyGateText(gate));
        setElementText('strategy-context-verified', active.last_verified_at ? `verified ${active.last_verified_at}` : '-');
        setElementText(
            'strategy-context-used',
            `${strategyOperationText(operation)} / backtest ${active.last_backtested_at || '-'} / paper ${active.last_paper_completed_at || active.last_paper_started_at || '-'}`
        );
    } catch (err) {
        console.error('Failed to render strategy context:', err);
    }
}

function strategyGateText(gate) {
    if (gate?.ok) return '검증 통과';
    const missing = (gate?.missing || []).map(strategyGateMissingLabel).join(', ');
    return missing ? `검증 필요: ${missing}` : '검증 필요';
}

function strategyGateMissingLabel(value) {
    const labels = {
        'static verification': '정적',
        'api verification': 'API',
        'backtest': '백테스트',
        'paper trading': '페이퍼',
        'active strategy': '활성전략'
    };
    return labels[value] || value;
}

function strategyOperationText(operation) {
    if (operation?.ready) {
        if (operation.mode === 'demo') return '운영 가능(DEMO)';
        return operation.mode === 'dry_run' ? '운영 가능(DRY_RUN)' : '운영 가능';
    }
    if (operation?.mode === 'inactive') return '미선택';
    return '운영 차단';
}

function strategyOperationKind(operation) {
    if (operation?.ready) return operation.mode === 'dry_run' ? 'warn' : 'buy';
    if (operation?.mode === 'inactive') return 'hold';
    return 'sell';
}

function summarizeCounts(counts) {
    return Object.entries(counts || {})
        .map(([key, value]) => `${key}:${value}`)
        .join(' / ') || '-';
}

function eventPayloadSummary(payload) {
    if (!payload) return '-';
    let data = payload;
    if (typeof payload === 'string') {
        try {
            data = JSON.parse(payload);
        } catch (_err) {
            return payload.slice(0, 180);
        }
    }
    if (data.message) return String(data.message);
    if (data.result?.message) return String(data.result.message);
    if (data.warnings?.length) return data.warnings.join(', ');
    if (data.gate?.missing?.length) return `missing ${data.gate.missing.join(', ')}`;
    if (data.performance?.candidate_count !== undefined) return `candidates ${data.performance.candidate_count}`;
    return JSON.stringify(data).slice(0, 180);
}

async function renderStrategyAudit(strategyId) {
    const id = strategyId || activeStrategyAuditId || document.getElementById('select-ai-ranker')?.value || '';
    if (!id) return;
    activeStrategyAuditId = id;
    try {
        const [performance, events, strategiesRes] = await Promise.all([
            fetchJson(`/api/ai-strategies/${encodeURIComponent(id)}/performance?days=30`, 30000),
            fetchJson(`/api/ai-strategies/${encodeURIComponent(id)}/events?limit=20`, 30000),
            fetchJson('/api/ai-strategies', 30000),
        ]);
        setElementText('strategy-audit-title', `${id} 최근 운영 상태`);
        setElementText('strategy-audit-candidates', formatNumber(performance.candidate_count || 0));
        setElementText(
            'strategy-audit-score',
            `${performance.avg_final_score ?? '-'} / ${performance.avg_rule_score ?? '-'} / ${performance.avg_ml_score ?? '-'}`
        );
        setElementText('strategy-audit-status', summarizeCounts(performance.ai_model_status_counts));
        setElementText('strategy-audit-optimizer', summarizeCounts(performance.optimizer_counts));
        const trades = performance.trade_summary || {};
        setElementText(
            'strategy-audit-review',
            `${performance.avg_return_5d ?? '-'}% / ${performance.win_rate_5d ?? '-'}%`
        );
        setElementText(
            'strategy-audit-warning',
            `5d return/win, fill ${trades.fill_rate ?? '-'}% (${trades.filled_count || 0}/${trades.order_count || 0})`
        );

        // Draw strategy backtest chart
        const strategy = (strategiesRes.strategies || []).find(s => s.id === id);
        let backtestData = null;
        if (strategy && strategy.last_validation_result) {
            try {
                const valResult = typeof strategy.last_validation_result === 'string'
                    ? JSON.parse(strategy.last_validation_result)
                    : strategy.last_validation_result;
                backtestData = valResult.checks?.backtest;
            } catch (err) {
                console.warn('Failed to parse last_validation_result:', err);
            }
        }

        const container = document.getElementById('strategy-backtest-chart-container');
        if (container) {
            if (backtestData && backtestData.equity_curve && backtestData.equity_curve.length > 0) {
                container.style.display = 'block';
                const ctx = document.getElementById('chart-strategy-backtest').getContext('2d');
                
                if (window.strategyBacktestChart) {
                    window.strategyBacktestChart.destroy();
                }
                
                const labels = backtestData.dates || backtestData.equity_curve.map((_, i) => `Day ${i}`);
                const dataPoints = backtestData.equity_curve;
                
                window.strategyBacktestChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: '누적 자산 가치',
                            data: dataPoints,
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.1,
                            pointRadius: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                mode: 'index',
                                intersect: false,
                                callbacks: {
                                    label: function(context) {
                                        return '자산: ' + Number(context.raw).toLocaleString() + '원';
                                    }
                                }
                            }
                        },
                        scales: {
                            x: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: { color: '#94a3b8', font: { size: 9 }, maxTicksLimit: 8 }
                            },
                            y: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: { color: '#94a3b8', font: { size: 9 } }
                            }
                        }
                    }
                });
            } else {
                container.style.display = 'none';
            }
        }

        const tbody = document.querySelector('#table-strategy-events tbody');
        if (tbody) {
            tbody.innerHTML = '';
            const rows = events.events || [];
            if (!rows.length) {
                setTableMessage('#table-strategy-events tbody', 4, '전략 이벤트가 없습니다.');
            } else {
                rows.forEach((event) => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${escapeHtml(event.ts || '-')}</td>
                        <td>${escapeHtml(event.event_type || '-')}</td>
                        <td>${escapeHtml(event.strategy_version || '-')}</td>
                        <td>${escapeHtml(eventPayloadSummary(event.payload))}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        }
    } catch (err) {
        setStatus(`전략 감사 조회 실패: ${err.message}`);
    }
}

async function renderAiStrategies() {
    const tbody = document.querySelector('#table-ai-strategies tbody');
    if (!tbody) return;
    try {
        const data = await fetchJson('/api/ai-strategies');
        tbody.innerHTML = '';
        const strategies = data.strategies || [];
        if (!strategies.length) {
            setTableMessage('#table-ai-strategies tbody', 6, '등록된 AI 전략이 없습니다.');
            return;
        }

        strategies.forEach((strategy) => {
            const tr = document.createElement('tr');
            const model = strategy.model === 'none' ? 'Local Rule' : strategy.model;
            const weight = Number(strategy.profile?.ai_weight ?? strategy.weight ?? 0);
            const builtIn = ['gpt_5_mini_default', 'rule_only_default'].includes(strategy.id);
            const gate = strategy.approval_gate || {};
            const operation = strategy.operation_status || {};
            const autonomy = strategy.autonomy || {};
            const validationSummary = gate.label || strategyGateText(gate);
            const operationSummary = strategyOperationLabel(operation);
            const operationReason = operation.reason_label || operation.reason || operationSummary;
            tr.innerHTML = `
                <td style="text-align:center;">
                    <input type="checkbox" class="strategy-select-checkbox" data-id="${escapeHtml(strategy.id)}" ${strategy.selected ? 'checked' : ''}>
                </td>
                <td>
                    <div class="symbol-name">${escapeHtml(strategyDisplayName(strategy))}</div>
                    <div class="symbol-code">${escapeHtml(strategy.id)} · ${escapeHtml(String(strategy.profile_hash || '').slice(0, 8))}</div>
                </td>
                <td>
                    ${pill(strategy.status_label || strategyStatusLabel(strategy.status), strategyStatusKind(strategy.status))}
                    ${pill(operationSummary, strategyOperationKind(operation))}
                    ${pill(autonomy.enabled ? '자율 ON' : '자율 OFF', autonomy.enabled ? 'buy' : 'hold')}
                </td>
                <td>${escapeHtml(model)}</td>
                <td>${pill(`${formatNumber(weight * 100, 0)}%`, weight > 0 ? 'buy' : 'hold')}</td>
                <td>
                    <div class="button-row">
                        <button type="button" class="button-ghost btn-quick-apply-strategy compact-button" data-id="${escapeHtml(strategy.id)}">적용</button>
                        <button type="button" class="button-ghost btn-quick-validate-strategy compact-button" data-id="${escapeHtml(strategy.id)}">자동검증</button>
                        <button type="button" class="button-ghost btn-performance-strategy compact-button" data-id="${escapeHtml(strategy.id)}">성과</button>
                        <button type="button" class="button-ghost btn-autonomy-run-strategy compact-button" data-id="${escapeHtml(strategy.id)}" ${autonomy.enabled && autonomy.applicable ? '' : 'disabled'}>자율 실행</button>
                        <button type="button" class="button-ghost btn-evolve-strategy compact-button" style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3);" data-id="${escapeHtml(strategy.id)}">🌱 자가진화</button>
                    </div>
                    <details class="strategy-advanced-actions">
                        <summary>고급 검증/운영</summary>
                        <div class="button-row">
                            <button type="button" class="button-ghost btn-static-verify-strategy compact-button" data-id="${escapeHtml(strategy.id)}">정적검증</button>
                            <button type="button" class="button-ghost btn-verify-strategy compact-button" data-id="${escapeHtml(strategy.id)}">API검증</button>
                            <button type="button" class="button-ghost btn-backtest-strategy compact-button" data-id="${escapeHtml(strategy.id)}">백테스트</button>
                            <button type="button" class="button-ghost btn-paper-start-strategy compact-button" data-id="${escapeHtml(strategy.id)}">페이퍼시작</button>
                            <button type="button" class="button-ghost btn-paper-complete-strategy compact-button" data-id="${escapeHtml(strategy.id)}">페이퍼완료</button>
                            <button type="button" class="button-ghost btn-approve-strategy compact-button" data-id="${escapeHtml(strategy.id)}">승인</button>
                            <button type="button" class="button-ghost btn-review-strategy compact-button" data-id="${escapeHtml(strategy.id)}">재검토</button>
                            <button type="button" class="button-ghost btn-retire-strategy compact-button" data-id="${escapeHtml(strategy.id)}">폐기</button>
                            <button type="button" class="button-danger btn-delete-strategy compact-button" data-id="${escapeHtml(strategy.id)}" ${builtIn ? 'disabled' : ''}>삭제</button>
                        </div>
                    </details>
                    <div class="time-muted">검증: ${escapeHtml(validationSummary)} / 운영: ${escapeHtml(operation.reason || operationSummary)}</div>
                </td>
            `;
            tbody.appendChild(tr);
        });

        tbody.querySelectorAll('.strategy-select-checkbox').forEach((input) => {
            input.addEventListener('change', async () => {
                const id = input.getAttribute('data-id');
                await postJson(`/api/ai-strategies/${id}/select`, { selected: input.checked });
                if (input.checked) localStorage.setItem('hanstock_ai_ranker', id);
                await Promise.all([syncStrategiesToDropdown(), renderStrategyContext(), renderAiStrategies()]);
                await renderStrategyAudit(id);
                setStatus(`AI 전략 ${input.checked ? '선택' : '해제'}: ${id}`, true);
            });
        });

        const bindStrategyAction = (selector, fn) => {
            tbody.querySelectorAll(selector).forEach((button) => {
                button.addEventListener('click', async () => {
                    const id = button.getAttribute('data-id');
                    setButtonBusy(button, true);
                    try {
                        await fn(id);
                        await Promise.all([syncStrategiesToDropdown(), renderStrategyContext(), renderAiStrategies()]);
                    } catch (err) {
                        setStatus(`전략 작업 실패: ${err.message}`);
                    } finally {
                        setButtonBusy(button, false);
                    }
                });
            });
        };
        bindStrategyAction('.btn-quick-apply-strategy', async (id) => {
            await postJson(`/api/ai-strategies/${id}/select`, { selected: true });
            localStorage.setItem('hanstock_ai_ranker', id);
            await renderStrategyAudit(id);
            setStatus('전략을 바로 적용했습니다.', true);
        });
        bindStrategyAction('.btn-quick-validate-strategy', async (id) => {
            const current = await fetchJson('/api/ai-strategies');
            const target = (current.strategies || []).find((strategy) => strategy.id === id) || {};
            await postJson(`/api/ai-strategies/${id}/static-verify`, {});
            if (target.provider !== 'none') {
                await postJson(`/api/ai-strategies/${id}/verify`, {});
            }
            const backtest = await postJson(`/api/ai-strategies/${id}/backtest`, {});
            let approveError = null;
            try {
                await postJson(`/api/ai-strategies/${id}/approve`, {});
            } catch (err) {
                approveError = err;
                // Approval may require optional paper/API gates for manually-created advanced strategies.
            }
            const updated = await fetchJson('/api/ai-strategies');
            const strategy = (updated.strategies || []).find((item) => item.id === id) || {};
            const gateText = strategyGateText(strategy.approval_gate || {});
            const status = backtest.result?.status || 'done';
            setStatus(`자동검증 완료: ${status} / ${gateText}${approveError ? ` (${approveError.message})` : ''}`, Boolean(backtest.result?.success) && !approveError);
        });
        bindStrategyAction('.btn-static-verify-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/static-verify`, {});
            setStatus(`정적 검증: ${result.result?.status || 'done'}`, Boolean(result.result?.ok));
        });
        bindStrategyAction('.btn-verify-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/verify`, {});
            setStatus(result.result?.message || 'API 검증 완료', Boolean(result.result?.success));
        });
        bindStrategyAction('.btn-backtest-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/backtest`, {});
            const metrics = result.result?.metrics || {};
            setStatus(`Backtest ${result.result?.status || 'done'} · PF ${metrics.profit_factor || '-'}`, Boolean(result.result?.success));
        });
        bindStrategyAction('.btn-paper-start-strategy', async (id) => {
            await postJson(`/api/ai-strategies/${id}/paper/start`, {});
            setStatus('Paper validation started.', true);
        });
        bindStrategyAction('.btn-paper-complete-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/paper/complete`, {
                days: 20,
                observations: 20,
                return_pct: 0,
                max_drawdown_pct: 0
            });
            setStatus(`Paper validation ${result.result?.status || 'done'}`, Boolean(result.result?.success));
        });
        bindStrategyAction('.btn-approve-strategy', async (id) => {
            await postJson(`/api/ai-strategies/${id}/approve`, {});
            setStatus('전략을 승인했습니다.', true);
        });
        bindStrategyAction('.btn-performance-strategy', async (id) => {
            await renderStrategyAudit(id);
            setStatus('전략 성과와 이벤트를 불러왔습니다.', true);
        });
        bindStrategyAction('.btn-autonomy-run-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${encodeURIComponent(id)}/autonomy/run`, {
                market: 'KR'
            });
            const autonomy = result.autonomy || {};
            const orderCount = (autonomy.managed_orders || []).length;
            const approvalCount = (autonomy.approvals || []).length;
            await renderStrategyAudit(id);
            setStatus(`자율전략 실행 완료 · 관리주문 ${orderCount}건 · 승인대기 ${approvalCount}건`, Boolean(result.ok));
        });
        bindStrategyAction('.btn-evolve-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/evolve`, {});
            const params = result.result?.params || {};
            const metrics = result.result?.metrics || {};
            setStatus(`🌱 자가진화 완료! 새 버전 파라미터 적용 - AI 비중: ${Math.round(params.ai_weight * 100)}%, 백테스트 수익률: ${metrics.total_return_pct}%`, true);
        });
        bindStrategyAction('.btn-review-strategy', async (id) => {
            const result = await postJson(`/api/ai-strategies/${id}/performance/review?days=30`, {});
            setElementText('strategy-audit-review', result.new_status || '-');
            setElementText('strategy-audit-warning', (result.warnings || []).join(', ') || '문제 없음');
            await renderStrategyAudit(id);
            setStatus(`전략 재검토 완료: ${result.previous_status} -> ${result.new_status}`, true);
        });
        bindStrategyAction('.btn-retire-strategy', async (id) => {
            await postJson(`/api/ai-strategies/${id}/retire`, {});
            setStatus('전략을 폐기 상태로 전환했습니다.', true);
        });
        bindStrategyAction('.btn-delete-strategy', async (id) => {
            if (!window.confirm('이 AI 전략을 삭제하시겠습니까?')) return;
            await deleteJson(`/api/ai-strategies/${id}`);
            setStatus('전략을 삭제했습니다.', true);
        });
        await renderStrategyAudit(activeStrategyAuditId || strategies.find((strategy) => strategy.selected)?.id || strategies[0]?.id);
    } catch (err) {
        setTableMessage('#table-ai-strategies tbody', 6, err.message);
    }
}

// 데이터 정렬 처리 유틸리티
function sortWatchlistData() {
    if (!watchlistSortKey) return;
    watchlistCache.sort((a, b) => {
        let valA = a[watchlistSortKey];
        let valB = b[watchlistSortKey];
        
        // 결측치 예외 처리 (정렬 방향 상관없이 가장 아래로 정렬)
        if (valA === null || valA === undefined) return watchlistSortAsc ? 1 : -1;
        if (valB === null || valB === undefined) return watchlistSortAsc ? -1 : 1;
        
        if (typeof valA === 'number' && typeof valB === 'number') {
            return watchlistSortAsc ? valA - valB : valB - valA;
        }
        
        valA = String(valA).toLowerCase();
        valB = String(valB).toLowerCase();
        if (valA < valB) return watchlistSortAsc ? -1 : 1;
        if (valA > valB) return watchlistSortAsc ? 1 : -1;
        return 0;
    });
}

function drawWatchlist() {
    const tbody = document.querySelector('#table-watchlist tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    
    // 데이터 정렬 수행
    sortWatchlistData();
    
    // 헤더 정렬 아이콘 그리기
    const thead = document.querySelector('#table-watchlist thead');
    if (thead) {
        thead.querySelectorAll('.sort-header').forEach(th => {
            const key = th.getAttribute('data-sort');
            const iconSpan = th.querySelector('.sort-icon');
            if (iconSpan) {
                if (key === watchlistSortKey) {
                    iconSpan.innerHTML = watchlistSortAsc ? '▲' : '▼';
                    iconSpan.style.color = '#34d399'; // 활성 정렬 컬럼은 강조
                } else {
                    iconSpan.innerHTML = '';
                    iconSpan.style.color = '';
                }
            }
        });
    }

    if (!watchlistCache.length) {
        setTableMessage('#table-watchlist tbody', 10, '등록된 관심 종목이 없습니다.');
        return;
    }
    
    watchlistCache.forEach((s, idx) => {
        const tr = document.createElement('tr');
        
        // 1. 현재가 및 등락률 포맷
        let priceHtml = `<span style="color: rgba(255,255,255,0.25); font-size: 0.8rem;">-</span>`;
        if (s.price !== null && s.price !== undefined) {
            let changeHtml = '';
            if (s.change_rate !== null && s.change_rate !== undefined) {
                const rate = Number(s.change_rate);
                if (rate > 0) {
                    changeHtml = `<span style="color: #f87171; font-size: 0.78rem; font-weight: bold; margin-left: 4px;">▲${rate.toFixed(2)}%</span>`;
                } else if (rate < 0) {
                    changeHtml = `<span style="color: #60a5fa; font-size: 0.78rem; font-weight: bold; margin-left: 4px;">▼${Math.abs(rate).toFixed(2)}%</span>`;
                } else {
                    changeHtml = `<span style="color: rgba(255,255,255,0.4); font-size: 0.78rem; margin-left: 4px;">0.00%</span>`;
                }
            }
            priceHtml = `<span style="font-weight: 500; color: #fff;">${formatNumber(s.price)}원</span>${changeHtml}`;
        }
        
        // 2. AI 스코어
        let scoreStr = `-`;
        if (s.score !== null && s.score !== undefined) {
            const score = Number(s.score);
            let badgeStyle = "background: rgba(255,255,255,0.1); color: #ccc;";
            if (score >= 3.0) {
                badgeStyle = "background: rgba(16, 185, 129, 0.2); color: #34d399; font-weight: bold; border: 1px solid rgba(16, 185, 129, 0.3);";
            } else if (score >= 2.0) {
                badgeStyle = "background: rgba(59, 130, 246, 0.2); color: #60a5fa; font-weight: bold; border: 1px solid rgba(59, 130, 246, 0.3);";
            } else if (score >= 1.0) {
                badgeStyle = "background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.25);";
            }
            scoreStr = `<span style="padding: 2px 8px; border-radius: 20px; font-size: 0.8rem; ${badgeStyle}">${score.toFixed(1)}점</span>`;
        }
        
        // 3. RSI 보조지표 뱃지화
        let rsiStr = `<span style="color: rgba(255,255,255,0.25); font-size: 0.8rem;">-</span>`;
        if (s.rsi !== null && s.rsi !== undefined) {
            const rsi = Number(s.rsi);
            let rsiBadgeStyle = "background: rgba(255,255,255,0.06); color: rgba(255,255,255,0.7); border: 1px solid rgba(255,255,255,0.1);";
            if (rsi <= 30) {
                rsiBadgeStyle = "background: rgba(245, 158, 11, 0.2); color: #fbbf24; font-weight: bold; border: 1px solid rgba(245, 158, 11, 0.35);";
            } else if (rsi >= 70) {
                rsiBadgeStyle = "background: rgba(239, 68, 68, 0.2); color: #f87171; font-weight: bold; border: 1px solid rgba(239, 68, 68, 0.35);";
            }
            rsiStr = `<span style="padding: 2px 6px; border-radius: 4px; font-size: 0.78rem; ${rsiBadgeStyle}">${rsi.toFixed(1)}</span>`;
        }
        
        // 4. 섹터 뱃지 스타일 지정
        const sectorStr = s.sector ? escapeHtml(s.sector) : "미분류";
        let sectorBadgeStyle = "background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.7); border: 1px solid rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        if (s.sector === "반도체") {
            sectorBadgeStyle = "background: rgba(52, 211, 153, 0.15); color: #34d399; border: 1px solid rgba(52, 211, 153, 0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        } else if (s.sector && (s.sector.includes("바이오") || s.sector.includes("제약"))) {
            sectorBadgeStyle = "background: rgba(244, 63, 94, 0.15); color: #f43f5e; border: 1px solid rgba(244, 63, 94, 0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        } else if (s.sector && s.sector.includes("2차전지")) {
            sectorBadgeStyle = "background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        } else if (s.sector && s.sector.includes("자동차")) {
            sectorBadgeStyle = "background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        } else if (s.sector && (s.sector.includes("금융") || s.sector.includes("은행") || s.sector.includes("증권") || s.sector.includes("생명보험") || s.sector.includes("손해보험") || s.sector.includes("지주") || s.sector.includes("투자"))) {
            sectorBadgeStyle = "background: rgba(167, 139, 250, 0.15); color: #a78bfa; border: 1px solid rgba(167, 139, 250, 0.25); padding: 2px 6px; border-radius: 4px; font-size: 0.78rem;";
        }
        const sectorHtml = `<span style="${sectorBadgeStyle}">${sectorStr}</span>`;

        // 5. 대표 조건 / 스코어 사유
        const reasonStr = s.reason ? escapeHtml(s.reason) : "분석 데이터 없음";
        
        // 6. 분석 최종 시각 콤팩트화
        const timeStr = s.updated_at
            ? (s.updated_at.includes(' ') ? s.updated_at.split(' ')[1].substring(0, 5) : s.updated_at)
            : '-';
        
        tr.innerHTML = `
            <td style="text-align: center; color: rgba(255,255,255,0.4);">${s.index}</td>
            <td style="font-weight: 600; color: #fff;">${escapeHtml(s.symbol)}</td>
            <td style="color: rgba(255,255,255,0.8);">${escapeHtml(s.name)}</td>
            <td style="text-align: center;">${sectorHtml}</td>
            <td style="text-align: right;">${priceHtml}</td>
            <td style="text-align: center;">${scoreStr}</td>
            <td style="text-align: center;">${rsiStr}</td>
            <td style="color: rgba(255,255,255,0.6); font-size: 0.85rem;" title="${reasonStr}">${reasonStr}</td>
            <td style="text-align: center; color: rgba(255,255,255,0.4); font-size: 0.8rem;">${escapeHtml(timeStr)}</td>
            <td style="text-align: center;">
                <button type="button" class="button-ghost btn-delete-watchlist compact-button"
                    data-symbol="${escapeHtml(s.symbol)}"
                    ${watchlistInherited ? 'disabled title="공용 관심종목을 상속 중입니다."' : ''}
                    style="background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.25); padding: 2px 8px; border-radius: 4px; font-size: 0.78rem; cursor: pointer;">삭제</button>
            </td>
        `;
        tbody.appendChild(tr);
    });
    
    tbody.querySelectorAll('.btn-delete-watchlist').forEach(btn => {
        btn.addEventListener('click', async () => {
            const symbol = btn.getAttribute('data-symbol');
            setButtonBusy(btn, true);
            try {
                const strategyId = getActiveStrategyId();
                const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
                await deleteJson(`/api/watchlist/${symbol}${query}`);
                setStatus(`관심 종목(${symbol})이 삭제되었습니다.`, true);
                await renderWatchlist();
            } catch (err) {
                setStatus(`관심 종목 삭제 실패: ${err.message}`);
                setButtonBusy(btn, false);
            }
        });
    });
}

async function renderWatchlist() {
    const autoChk = document.getElementById('chk-watchlist-ai-auto');
    
    // 테이블 헤더에 이벤트 리스너 바인딩 (최초 1회 실행)
    const thead = document.querySelector('#table-watchlist thead');
    if (thead && !thead.dataset.listenerBound) {
        thead.dataset.listenerBound = 'true';
        thead.querySelectorAll('.sort-header').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.getAttribute('data-sort');
                if (watchlistSortKey === key) {
                    watchlistSortAsc = !watchlistSortAsc;
                } else {
                    watchlistSortKey = key;
                    watchlistSortAsc = true;
                }
                drawWatchlist();
            });
        });
    }

    try {
        const strategyId = getActiveStrategyId();
        const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
        const data = await fetchJson(`/api/watchlist${query}`);
        watchlistInherited = Boolean(data.inherited);
        watchlistCache = data.symbols || [];
        watchlistCache.forEach((s, idx) => {
            s.index = idx + 1;
        });
        
        if (autoChk) {
            autoChk.checked = data.ai_auto_add;
        }
        const threshInput = document.getElementById('num-watchlist-ai-threshold');
        if (threshInput && data.ai_auto_add_threshold !== undefined) {
            threshInput.value = data.ai_auto_add_threshold;
        }
        
        drawWatchlist();
    } catch (err) {
        console.error("Failed to render watchlist:", err);
        setStatus(`관심종목 갱신 일시 실패 (기존 데이터 보존됨): ${err.message}`);
    }
}

async function renderSignals() {
    setButtonBusy('btn-signals', true);
    setTableMessage('#table-signals tbody', 7, '보유 종목을 진단하고 있습니다...');
    try {
        const strategyId = getActiveStrategyId();
        const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
        const data = await fetchJson(`/api/signals${query}`);
        const tbody = document.querySelector('#table-signals tbody');
        tbody.innerHTML = '';
        if (!data.signals.length) {
            setTableMessage('#table-signals tbody', 7, '보유 종목이 없습니다');
            return;
        }

        data.signals.forEach((row) => {
            const action = String(row.action || 'hold').toLowerCase();
            const kind = action === 'buy' ? 'buy' : (action === 'sell' ? 'sell' : 'hold');
            const queueButton = action === 'hold'
                ? `<button type="button" class="button-ghost" disabled title="관망 신호이므로 주문할 내역이 없습니다." style="opacity:0.3; cursor:not-allowed;">보유(관망)</button>`
                : `<button type="button" class="button-ghost queue-order"
                    data-symbol="${escapeHtml(row.symbol)}"
                    data-name="${escapeHtml(row.name)}"
                    data-action="${escapeHtml(action)}"
                    data-qty="${Number(row.signal_qty || 0)}"
                    data-price="${Number(row.signal_price || 0)}"
                    data-reason="${escapeHtml(row.reason)}"
                    data-source="signal">승인대기</button>`;
            const reason = translateReason(row.reason);
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <div class="symbol-name">${escapeHtml(row.name)}</div>
                    <div class="symbol-code">${escapeHtml(row.symbol)}</div>
                </td>
                <td>${pill(toKorAction(action), kind)}</td>
                <td>${pill(formatNumber(row.strategy_score), Number(row.strategy_score || 0) >= 5 ? 'buy' : 'hold')}</td>
                <td>${Number(row.signal_qty || 0).toLocaleString()}</td>
                <td>${formatNumber(row.rsi, 1)} / ${formatNumber(row.rsi2, 1)}</td>
                <td>${formatNumber(row.macd_hist, 2)}</td>
                <td>
                    <div class="reason-cell" title="${escapeHtml(reason)}">${escapeHtml(reason)}</div>
                    ${queueButton}
                </td>
            `;
            tbody.appendChild(tr);
        });
        bindQueueButtons();
    } catch (err) {
        setTableMessage('#table-signals tbody', 7, err.message);
    } finally {
        setButtonBusy('btn-signals', false);
    }
}

async function renderCandidates() {
    setButtonBusy('btn-candidates', true);
    setTableMessage('#table-candidates tbody', 9, '관심종목에서 매수 후보를 찾고 있습니다...');
    try {
        const strategyId = getActiveStrategyId();
        const optimizer = document.getElementById('select-portfolio-optimizer')?.value || 'score_tilted_inverse_vol';
        const query = strategyId
            ? `/api/candidates?min_score=2&strategy_id=${encodeURIComponent(strategyId)}&optimizer=${encodeURIComponent(optimizer)}`
            : `/api/candidates?min_score=2&ranker=rule_only&optimizer=${encodeURIComponent(optimizer)}`;
        const data = await fetchJson(query, 90000);
        const tbody = document.querySelector('#table-candidates tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!data.candidates.length) {
            const scanned = data.scanned || 0;
            const scanError = data.scan_error || null;
            const tableMsg = scanned === 0
                ? (scanError ? `데이터 수신 실패 — 잠시 후 다시 시도해 주세요` : '분석 대상 종목이 없습니다')
                : `조건을 만족한 후보가 없습니다 — ${scanned}종목 분석 완료`;
            setTableMessage('#table-candidates tbody', 9, tableMsg);
            // 분석 근거 팝업
            const titleEl = document.getElementById('noCandidatesTitle');
            const subtitleEl = document.getElementById('noCandidatesSubtitle');
            const bodyEl = document.getElementById('noCandidatesBody');
            if (scanned === 0 && scanError) {
                if (titleEl) titleEl.textContent = '⚠️ 데이터 수신 실패';
                if (subtitleEl) subtitleEl.textContent = '시세 데이터를 가져오지 못해 분석을 진행할 수 없었습니다.';
                if (bodyEl) bodyEl.innerHTML = buildScanErrorModalMarkup(scanError);
            } else {
                if (titleEl) titleEl.textContent = '📊 매수 후보 없음 — 분석 결과';
                if (subtitleEl) subtitleEl.textContent =
                    `${scanned}종목을 분석했으나 기준 점수(${data.min_score || 2}점) 이상인 종목이 없습니다.`;
                if (bodyEl) bodyEl.innerHTML = buildNoCandidatesModalMarkup(data);
            }
            setNoCandidatesModalOpen(true);
            if (data._cache?.cached_at) {
                setStatus(`최근 후보 검색 결과를 표시합니다. 기준 시각 ${data._cache.cached_at}`, true);
            } else {
                setStatus('분석 완료 — 매수 기준을 충족하는 종목이 없습니다.', true);
            }
            return;
        }

        const displayedCandidates = data.candidates.slice(0, 10);
        displayedCandidates.forEach((row) => {
            const stockName = row.name && row.name !== row.ticker ? row.name : '';
            const queueButton = Number(row.planned_qty || 0) > 0
                ? `<button type="button" class="button-ghost queue-order"
                    data-symbol="${escapeHtml(row.ticker)}"
                    data-name="${escapeHtml(row.name || row.ticker)}"
                    data-action="buy"
                    data-qty="${Number(row.planned_qty || 0)}"
                    data-price="${Number(row.limit_price || row.current_price || 0)}"
                    data-reason="${escapeHtml((row.reasons || []).join(', '))}"
                    data-source="candidate">승인대기</button>`
                : `<button type="button" class="button-ghost" disabled title="잔고 부족 또는 최대 보유 종목 수(MAX_POSITIONS) 초과로 매수할 수 없습니다." style="opacity:0.5; cursor:not-allowed;">승인불가</button>`;

            // 상세 근거 빌드
            const reasonLines = (row.reasons || []).map(r => strategyReasonLabel(r));
            const detailParts = [];
            if (row.rsi != null) detailParts.push(`RSI ${formatNumber(row.rsi,1)}`);
            if (row.rsi2 != null) detailParts.push(`RSI2 ${formatNumber(row.rsi2,1)}`);
            if (row.macd_hist != null) detailParts.push(`MACD ${formatNumber(row.macd_hist,2)}`);
            if (row.sma20 != null && row.sma60 != null) {
                const trend = row.sma20 > row.sma60 ? '단기↑중기선 위' : '단기↓중기선 아래';
                detailParts.push(trend);
            }
            if (row.bb_lo != null && row.current_price != null) {
                const bbDist = ((row.current_price - row.bb_lo) / row.bb_lo * 100).toFixed(1);
                detailParts.push(`볼밴하단+${bbDist}%`);
            }
            const detailSuffix = detailParts.length ? ` (${detailParts.join(' | ')})` : '';
            const reasonText = reasonLines.join(' · ') + detailSuffix;

            const promotedBadge = row.promoted_by_ai 
                ? `<span class="badge-purple" style="background-color: #8b5cf6; color: white; padding: 1px 5px; border-radius: 3px; font-size: 0.72rem; font-weight: 600; display: inline-block; vertical-align: middle; margin-left: 4px; box-shadow: 0 0 4px rgba(139, 92, 246, 0.4);">AI 승격</span>`
                : '';

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <span class="symbol-name">${escapeHtml(stockName || row.ticker)}${promotedBadge}</span>
                    <span class="symbol-code">${stockName ? row.ticker : ''}</span>
                </td>
                <td>${pill(formatNumber(row.score, 2), row.score >= 3 ? 'buy' : 'warn')}</td>
                <td>${buildCandidateStrategyMarkup(row)}</td>
                <td>${formatNumber(row.rsi, 1)} / ${formatNumber(row.rsi2, 1)}</td>
                <td>${formatNumber(row.macd_hist, 2)}</td>
                <td>${formatCurrency(row.current_price)}</td>
                <td>${Number(row.planned_qty || 0).toLocaleString()}</td>
                <td>${formatCurrency(row.estimated_cost)}</td>
                <td>
                    <div class="reason-detail">${escapeHtml(reasonText)}</div>
                    ${queueButton}
                </td>
            `;
            const rowQueueButton = tr.querySelector('.queue-order');
            if (rowQueueButton) {
                rowQueueButton.dataset.strategyId = row.strategy_id || '';
                rowQueueButton.dataset.strategyVersion = row.strategy_version || '';
                rowQueueButton.dataset.profileHash = row.profile_hash || '';
                rowQueueButton.dataset.sourceCandidateId = row.id || '';
            }
            tbody.appendChild(tr);
        });
        bindQueueButtons();
        await renderCandidateHistory();
        if (data._cache?.cached_at) {
            setStatus(`최근 후보 검색 결과를 표시합니다. 기준 시각 ${data._cache.cached_at}`, true);
        } else {
            setStatus('매수 후보 검색을 완료했습니다.', true);
        }
    } catch (err) {
        setTableMessage('#table-candidates tbody', 9, err.message);
    } finally {
        setButtonBusy('btn-candidates', false);
    }
}

async function renderCandidateHistory() {
    try {
        const data = await fetchJson('/api/candidates/history?limit=50', 30000);
        const tbody = document.querySelector('#table-candidates-history tbody');
        if (!tbody) return;
        
        tbody.innerHTML = '';
        const historyList = data.history || [];
        if (!historyList.length) {
            tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 2rem; color: #94a3b8;">포착된 매수후보 기록이 없습니다.</td></tr>`;
            return;
        }
        
        historyList.forEach(item => {
            const tr = document.createElement('tr');
            
            const rsiVal = item.rsi != null ? `RSI ${Number(item.rsi).toFixed(1)}` : '';
            const rsi2Val = item.rsi2 != null ? `RSI2 ${Number(item.rsi2).toFixed(1)}` : '';
            const macdVal = item.macd_hist != null ? `MACD ${Number(item.macd_hist).toFixed(2)}` : '';
            const sma20 = item.sma20 || 0;
            const sma60 = item.sma60 || 0;
            const smaVal = sma20 > 0 && sma60 > 0 ? (sma20 > sma60 ? '단기↑중기선 위' : '단기↓중기선 아래') : '';
            const indicatorParts = [rsiVal, rsi2Val, macdVal, smaVal].filter(Boolean);
            const indicatorText = indicatorParts.length ? indicatorParts.join(' | ') : '-';
            
            const reasonsText = (item.reasons || '').split(',').map(r => strategyReasonLabel(r)).join(' · ');
            const envText = item.env === 'real' ? pill('실전', 'sell') : pill('모의', 'hold');
            
            tr.innerHTML = `
                <td><strong>${escapeHtml(item.scanned_at)}</strong></td>
                <td>
                    <span class="symbol-name">${escapeHtml(item.name || item.symbol)}</span>
                    <span class="symbol-code">${item.symbol}</span>
                </td>
                <td>${pill(formatNumber(item.score, 2), item.score >= 3 ? 'buy' : 'warn')}</td>
                <td>${formatCurrency(item.price)}</td>
                <td><small style="color: #94a3b8;">${escapeHtml(indicatorText)}</small></td>
                <td><div class="reason-cell" title="${escapeHtml(reasonsText)}">${escapeHtml(reasonsText)}</div></td>
                <td>${envText}</td>
                <td>
                    <button type="button" class="button-ghost delete-candidate-history" data-id="${item.id}" style="color: #ef4444; border-color: rgba(239, 68, 68, 0.2); padding: 4px 8px; font-size: 0.8rem; height: auto; min-height: auto;">삭제</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
        
        const deleteButtons = tbody.querySelectorAll('.delete-candidate-history');
        deleteButtons.forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const id = btn.dataset.id;
                if (!id) return;
                if (confirm('이 매수후보 포착 기록을 데이터베이스에서 삭제하시겠습니까?')) {
                    try {
                        const res = await fetchJson(`/api/candidates/history/${id}`, 10000, { method: 'DELETE' });
                        if (res.ok) {
                            setStatus('매수후보 포착 기록이 성공적으로 삭제되었습니다.', true);
                            await renderCandidateHistory();
                        }
                    } catch (err) {
                        console.error('Failed to delete candidate history', err);
                        alert('삭제 처리 중 오류가 발생했습니다: ' + err.message);
                    }
                }
            });
        });
        
    } catch (err) {
        console.error('Failed to fetch candidate history', err);
        setTableMessage('#table-candidates-history tbody', 8, err.message);
    }
}

async function renderAiAllocation() {
    setButtonBusy('btn-ai-allocation', true);
    setTableMessage('#table-ai-allocation tbody', 8, 'AI 목표 비중을 계산하고 있습니다...');
    try {
        const data = await fetchJson('/api/ai-allocation', 45000);
        const tbody = document.querySelector('#table-ai-allocation tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!data.positions.length) {
            setTableMessage('#table-ai-allocation tbody', 8, '계산할 보유 종목이 없습니다');
            return;
        }

        data.positions.forEach((row) => {
            const action = String(row.rebalance_action || 'hold').toLowerCase();
            const kind = action === 'buy' ? 'buy' : (action === 'sell' ? 'sell' : 'hold');
            const reason = `AI 목표비중 ${formatNumber(row.target_weight * 100, 1)}%; ${translateReason(((row.reasons || []).slice(0, 3)).join(', '))}`;
            const modalPayload = encodeURIComponent(JSON.stringify({
                symbol: row.symbol,
                name: row.name,
                action,
                score: Number(row.score || 0),
                currentWeight: Number(row.current_weight || 0),
                targetWeight: Number(row.target_weight || 0),
                deltaValue: Number(row.delta_value || 0),
                volatility: Number(row.volatility || 0),
                reasoning_kr: row.reasoning_kr || '',
                ai_strategy_name: row.ai_strategy_name || 'AI 전략 상세',
                reasons: Array.isArray(row.reasons) ? row.reasons : []
            }));
            const queueButton = action === 'hold'
                ? `<button type="button" class="button-ghost" disabled title="AI가 현재 비중을 유지할 것을 권장합니다." style="opacity:0.3; cursor:not-allowed;">유지</button>`
                : `<button type="button" class="button-ghost queue-order"
                    data-symbol="${escapeHtml(row.symbol)}"
                    data-name="${escapeHtml(row.name)}"
                    data-action="${escapeHtml(action)}"
                    data-qty="${Number(row.rebalance_qty || 0)}"
                    data-price="${Number(row.price || 0)}"
                    data-reason="${escapeHtml(reason)}"
                    data-source="ai-allocation"
                    data-strategy-id="${escapeHtml(row.strategy_id || '')}"
                    data-strategy-version="${escapeHtml(row.strategy_version || '')}"
                    data-profile-hash="${escapeHtml(row.profile_hash || '')}">승인대기</button>`;
            const tr = document.createElement('tr');
            const aiReasonText = String(row.reasoning_kr || row.reasons?.join(', ') || '-');
            tr.innerHTML = `
                <td>
                    <div class="symbol-name">${escapeHtml(row.name)}</div>
                    <div class="symbol-code">${escapeHtml(row.symbol)}</div>
                </td>
                <td>${pill(formatNumber(row.score, 2), Number(row.score || 0) > 0 ? 'buy' : 'hold')}</td>
                <td>${formatNumber(row.current_weight * 100, 1)}%</td>
                <td>${formatNumber(row.target_weight * 100, 1)}%</td>
                <td>${formatCurrency(row.delta_value)}</td>
                <td>${pill(toKorAction(action), kind)}</td>
                <td>
                    <button type="button" class="clickable-reason"
                        data-ai-payload="${modalPayload}"
                        data-reason="${escapeHtml(aiReasonText)}"
                        onclick="showAiModal(this)">
                        ${escapeHtml(row.ai_strategy_name || "전략 상세 내역 보기")}
                    </button>
                </td>
                <td>${queueButton}</td>
            `;
            tbody.appendChild(tr);
        });
        bindQueueButtons();
    } catch (err) {
        setTableMessage('#table-ai-allocation tbody', 8, err.message);
    } finally {
        setButtonBusy('btn-ai-allocation', false);
    }
}

function isHoldingSellPayload(payload) {
    return payload.action === 'sell'
        && (payload.source === 'dashboard_holding_sell' || payload.source === 'mistock_holding_sell');
}

function removeQueuedHoldingRow(button, payload) {
    if (!isHoldingSellPayload(payload)) {
        return;
    }
    holdingsCache = holdingsCache.filter((holding) => String(holding.symbol) !== String(payload.symbol));
    const row = button.closest('tr');
    if (row) {
        row.remove();
    }
    const tbody = document.querySelector('#table-holdings tbody');
    if (tbody && !tbody.querySelector('tr')) {
        setTableMessage('#table-holdings tbody', 8, 'Sell request is now tracked in the orders tab');
    }
}

function removeSellableHoldingRows() {
    holdingsCache = holdingsCache.filter((holding) => {
        const qty = Number(holding.qty || 0);
        const sellableQty = Number(holding.sellable_qty ?? holding.qty ?? 0);
        return !(qty > 0 && sellableQty > 0);
    });
    renderHoldingRows();
}

function showOrdersTab() {
    const tabEl = document.querySelector('[data-dashboard-tab="orders"]');
    if (tabEl) {
        tabEl.click();
    }
}

function scheduleOrderProgressRefresh() {
    setTimeout(() => {
        renderApprovals();
        renderTrades();
    }, 1500);
    setTimeout(() => {
        renderApprovals();
        renderTrades();
        renderBalance();
    }, 5000);
}

async function createApprovalFromButton(button) {
    const payload = {
        symbol: button.dataset.symbol,
        name: button.dataset.name,
        action: button.dataset.action,
        qty: Number(button.dataset.qty || 0),
        price: Number(button.dataset.price || 0),
        reason: button.dataset.reason || '',
        source: button.dataset.source || 'dashboard',
        strategy_id: button.dataset.strategyId || '',
        strategy_version: Number(button.dataset.strategyVersion || 0) || null,
        profile_hash: button.dataset.profileHash || '',
        source_candidate_id: Number(button.dataset.sourceCandidateId || 0) || null
    };
    button.disabled = true;
    try {
        const result = await postJson('/api/approvals', payload);
        removeQueuedHoldingRow(button, payload);
        await renderApprovals();
        if (isHoldingSellPayload(payload)) {
            showOrdersTab();
            scheduleOrderProgressRefresh();
        }
        if (result.auto_approved) {
            setStatus(`${toKorAction(payload.action)} ${payload.symbol} 주문을 자동승인 처리했습니다.`, result.status !== 'failed');
            await Promise.all([renderTrades(), renderBalance()]);
        } else {
            setStatus(`${toKorAction(payload.action)} ${payload.symbol} 주문을 승인 대기에 올렸습니다.`, true);
        }
    } catch (err) {
        setStatus(`승인 대기 등록 실패: ${err.message}`);
        button.disabled = false;
    }
}

function bindQueueButtons() {
    document.querySelectorAll('.queue-order').forEach((button) => {
        button.addEventListener('click', () => createApprovalFromButton(button), { once: true });
    });
}

async function sellAllHoldings() {
    const button = document.getElementById('btn-sell-all-holdings');
    if (!window.confirm('현재 보유 종목을 전량 시장가 매도 승인으로 등록할까요?')) {
        return;
    }
    if (button) {
        button.disabled = true;
    }
    try {
        const result = await postJson('/api/holdings/sell-all', {});
        if (result.status === 'empty') {
            setStatus('매도할 보유 종목이 없습니다.', true);
            return;
        }
        const submittedCount = result.submitted_count ?? result.executed_count ?? 0;
        const skippedCount = result.skipped_count || 0;
        const details = `대기 ${result.pending_count || 0}건, 주문접수 ${submittedCount}건, 실패 ${result.failed_count || 0}건, 제외 ${skippedCount}건`;
        const fillNote = result.fill_status_note || '실제 체결 여부는 주문내역 동기화 후 확정됩니다.';
        setStatus(
            `전량 매도 요청 ${result.created_count || 0}건을 등록했습니다. ${details}. ${fillNote}`,
            (result.failed_count || 0) === 0
        );
        removeSellableHoldingRows();
        showOrdersTab();
        scheduleOrderProgressRefresh();
        await Promise.all([renderApprovals(), renderTrades()]);
    } catch (err) {
        setStatus(`전량 매도 요청 실패: ${err.message}`);
    } finally {
        if (button) {
            button.disabled = false;
        }
    }
}

async function processOptimizerBatch() {
    const buttons = document.querySelectorAll('#table-optimizer tbody .queue-order:not([disabled])');
    if (buttons.length === 0) {
        alert('일괄 처리할 주문 제안이 없습니다.');
        return;
    }

    if (!window.confirm(`최적화 제안 ${buttons.length}건의 주문을 일괄 승인 대기로 등록하시겠습니까?`)) {
        return;
    }

    const batchButton = document.getElementById('btn-optimizer-batch');
    if (batchButton) {
        batchButton.disabled = true;
    }

    let successCount = 0;
    let failCount = 0;

    const promises = Array.from(buttons).map(async (button) => {
        const payload = {
            symbol: button.dataset.symbol,
            name: button.dataset.name,
            action: button.dataset.action,
            qty: Number(button.dataset.qty || 0),
            price: Number(button.dataset.price || 0),
            reason: button.dataset.reason || '',
            source: button.dataset.source || 'dashboard',
            strategy_id: button.dataset.strategyId || '',
            strategy_version: Number(button.dataset.strategyVersion || 0) || null,
            profile_hash: button.dataset.profileHash || '',
            source_candidate_id: Number(button.dataset.sourceCandidateId || 0) || null
        };
        button.disabled = true;
        try {
            const isMistock = window.location.pathname.includes('/mistock');
            const url = isMistock ? '/api/mistock/approvals' : '/api/approvals';
            await postJson(url, payload);
            successCount++;
            button.textContent = '등록완료';
            button.className = 'button-ghost';
            button.disabled = true;
        } catch (err) {
            failCount++;
            button.disabled = false;
            console.error(`Batch order registration failed for ${payload.symbol}:`, err);
        }
    });

    try {
        await Promise.all(promises);
        setStatus(`최적화 일괄 등록 완료 (성공: ${successCount}건, 실패: ${failCount}건)`, failCount === 0);
    } catch (err) {
        setStatus(`최적화 일괄 처리 중 오류 발생: ${err.message}`);
    } finally {
        if (batchButton) {
            batchButton.disabled = false;
        }
    }
    // Refresh UI in the background to prevent button from hanging on slow KIS API calls
    renderApprovals();
    renderTrades();
    renderBalance();
}

async function renderApprovals() {
    try {
        const data = await fetchJson('/api/approvals?limit=50');
        const tbody = document.querySelector('#table-approvals tbody');
        if (!tbody) return;
        tbody.innerHTML = '';
        if (!data.approvals.length) {
            setTableMessage('#table-approvals tbody', 9, '승인 대기 주문이 없습니다');
            return;
        }

        data.approvals.forEach((row) => {
            const status = String(row.status || '');
            const statusKind = status === 'pending' ? 'warn' : (status === 'executed' ? 'buy' : (status === 'failed' ? 'sell' : 'hold'));
            const estimatedCost = Number(row.qty || 0) * Number(row.price || 0);
            const autoApprovalInProgress = Boolean(row.auto_approval_in_progress);
            const retryButton = row.retry_eligible
                ? `<button type="button" class="retry-approval" data-id="${row.id}">재처리</button>
                   <button type="button" class="button-danger cancel-retry-approval" data-id="${row.id}">취소후재처리</button>`
                : '';
            const responseText = escapeHtml(row.response_msg || row.order_status || '');
            const controls = status === 'pending' && autoApprovalInProgress
                ? `<span class="time-muted">자동승인 진행중</span>`
                : status === 'pending'
                ? `<div class="button-row">
                    <button type="button" class="approve-order" data-id="${row.id}">승인</button>
                    <button type="button" class="button-danger reject-order" data-id="${row.id}">거절</button>
                   </div>`
                : `<div class="button-row">
                    <span class="time-muted">${responseText}</span>
                    ${retryButton}
                   </div>`;

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>#${escapeHtml(row.id || '-')}</td>
                <td>
                    <div>${escapeHtml(String(row.created_at || '').split(' ')[0])}</div>
                    <div class="time-muted">${escapeHtml(String(row.created_at || '').split(' ')[1] || '')}</div>
                </td>
                <td>${pill(toKorAction(row.action), row.action === 'buy' ? 'buy' : 'sell')}</td>
                <td>
                    <div class="symbol-name">${escapeHtml(row.name || row.symbol)}</div>
                    <div class="symbol-code">${escapeHtml(row.symbol)}</div>
                </td>
                <td>${Number(row.qty || 0).toLocaleString()}</td>
                <td>${formatCurrency(row.price)}</td>
                <td>${formatCurrency(estimatedCost)}</td>
                <td>${pill(toKorStatus(status), statusKind)}</td>
                <td>${controls}</td>
            `;
            tbody.appendChild(tr);
        });

        document.querySelectorAll('.approve-order').forEach((button) => {
            button.addEventListener('click', () => handleApprovalAction(button, 'approve'));
        });
        document.querySelectorAll('.reject-order').forEach((button) => {
            button.addEventListener('click', () => handleApprovalAction(button, 'reject'));
        });
        document.querySelectorAll('.retry-approval').forEach((button) => {
            button.addEventListener('click', () => executeApprovalAction(button, 'retry'));
        });
        document.querySelectorAll('.cancel-retry-approval').forEach((button) => {
            button.addEventListener('click', () => executeApprovalAction(button, 'cancel-retry'));
        });
    } catch (err) {
        setTableMessage('#table-approvals tbody', 9, err.message);
    }
}

let pendingApprovalButton = null;
let pendingApprovalAction = null;

async function executeApprovalAction(button, action) {
    button.disabled = true;
    try {
        const result = await postJson(`/api/approvals/${button.dataset.id}/${action}`, {});
        setStatus(`승인 처리 결과: ${toKorStatus(result.status)} #${result.id}`, result.status !== 'failed');
        await Promise.all([renderApprovals(), renderTrades(), renderBalance()]);
    } catch (err) {
        setStatus(`승인 처리 실패: ${err.message}`);
        button.disabled = false;
    }
}

async function handleApprovalAction(button, action) {
    // 안드로이드 하이브리드 앱 내부이며 승인(approve)을 시도할 경우, 네이티브 생체 인식 요구
    if (typeof window.androidApp !== 'undefined' && action === 'approve') {
        button.disabled = true;
        pendingApprovalButton = button;
        pendingApprovalAction = action;
        setStatus("주문 실행을 위해 기기의 지문 또는 Face ID 생체 인증을 진행해 주세요.");
        window.androidApp.authenticateBiometric();
    } else {
        await executeApprovalAction(button, action);
    }
}

// 안드로이드 네이티브 생체 인증 완료 시 호출되는 전역 콜백
window.onBiometricResult = function(success) {
    if (success) {
        if (pendingApprovalButton && pendingApprovalAction) {
            setStatus("생체 인증 완료. 주문 처리를 요청합니다...", true);
            executeApprovalAction(pendingApprovalButton, pendingApprovalAction);
            pendingApprovalButton = null;
            pendingApprovalAction = null;
        }
    } else {
        if (pendingApprovalButton) {
            pendingApprovalButton.disabled = false;
            setStatus("생체 인증이 실패했거나 취소되어 주문 전송이 중단되었습니다.");
            pendingApprovalButton = null;
            pendingApprovalAction = null;
        }
    }
};

// FCM 알림 클릭 시 특정 대시보드 탭으로 즉시 라우팅하는 전역 콜백
window.routeToTab = function(tabName) {
    console.log("routeToTab received tab:", tabName);
    let target = tabName;
    if (tabName === 'approval' || tabName === 'approvals') {
        target = 'orders';
    }
    const tabEl = document.querySelector(`[data-dashboard-tab="${target}"]`);
    if (tabEl) {
        tabEl.click();
        setStatus(`FCM 알림 딥링크 라우팅: [${tabEl.textContent}] 탭으로 전환되었습니다.`, true);
    }
};


async function renderTrades() {
    try {
        // 성과 요약 (Performance)
        try {
            const perf = await fetchJson('/api/performance', 30000);
            document.getElementById('perf-total-trades').textContent = `${perf.total_trades}회`;
            document.getElementById('perf-success-rate').textContent = `${perf.success_rate}%`;
            
            const pnlEl = document.getElementById('perf-realized-pnl');
            pnlEl.textContent = formatCurrency(perf.realized_pnl);
            pnlEl.className = perf.realized_pnl > 0 ? 'text-success' : (perf.realized_pnl < 0 ? 'text-danger' : '');
            
            const evalPnlEl = document.getElementById('perf-eval-pnl');
            if (evalPnlEl) {
                const evalPnl = perf.total_eval_pnl || 0;
                evalPnlEl.textContent = formatCurrency(evalPnl);
                evalPnlEl.className = evalPnl > 0 ? 'text-success' : (evalPnl < 0 ? 'text-danger' : '');
            }
            
            const tbodyEval = document.querySelector('#table-eval-details tbody');
            if (tbodyEval) {
                tbodyEval.innerHTML = '';
                const details = perf.eval_details || [];
                if (!details.length) {
                    setTableMessage('#table-eval-details tbody', 6, '자동매매로 매수한 보유종목이 없습니다.');
                } else {
                    details.forEach((item) => {
                        const tr = document.createElement('tr');
                        const pnlClass = item.eval_pnl > 0 ? 'text-success' : (item.eval_pnl < 0 ? 'text-danger' : '');
                        tr.innerHTML = `
                            <td>
                                <span class="symbol-name">${escapeHtml(item.name || item.symbol)}</span>
                                ${item.diff_reason ? `<div style="font-size: 0.75rem; color: #ffc107; margin-top: 2px;">⚠️ ${escapeHtml(item.diff_reason)}</div>` : ''}
                            </td>
                            <td>${Number(item.qty || 0).toLocaleString()}</td>
                            <td>${formatCurrency(item.avg_cost)}</td>
                            <td>${formatCurrency(item.current_price)}</td>
                            <td>${formatCurrency(Number(item.current_price || 0) * Number(item.qty || 0))}</td>
                            <td class="${pnlClass}">${formatPercent(item.return_rate)}</td>
                            <td class="${pnlClass}">${item.eval_pnl > 0 ? '+' : ''}${formatCurrency(item.eval_pnl)}</td>
                        `;
                        tbodyEval.appendChild(tr);
                    });
                }
            }

            const diffContainer = document.getElementById('pnl-diff-container');
            const diffList = document.getElementById('pnl-diff-list');
            const brokerPnlSpan = document.getElementById('perf-broker-pnl');
            
            if (diffContainer && diffList && brokerPnlSpan && typeof perf.total_broker_pnl !== 'undefined') {
                const autoPnl = perf.total_eval_pnl || 0;
                const brokerPnl = perf.total_broker_pnl || 0;
                
                if (autoPnl !== brokerPnl) {
                    diffContainer.hidden = false;
                    brokerPnlSpan.textContent = formatCurrency(brokerPnl);
                    
                    let diffHtml = '';
                    const details = perf.eval_details || [];
                    details.forEach(item => {
                        if (item.diff_reason) {
                            const diffAmt = (item.broker_pnl || 0) - (item.eval_pnl || 0);
                            const sign = diffAmt > 0 ? '+' : '';
                            diffHtml += `<li><strong>${escapeHtml(item.name)}</strong>: ${escapeHtml(item.diff_reason)} (평가손익 차액: ${sign}${formatCurrency(diffAmt)})</li>`;
                        }
                    });
                    
                    const untracked = perf.untracked_details || [];
                    untracked.forEach(item => {
                        const sign = item.broker_pnl > 0 ? '+' : '';
                        diffHtml += `<li><strong>${escapeHtml(item.name)}</strong>: ${escapeHtml(item.diff_reason)} (증권사 평가손익 전체 합산: ${sign}${formatCurrency(item.broker_pnl)})</li>`;
                    });
                    
                    diffList.innerHTML = diffHtml || '<li>차이 원인을 분석할 수 없는 오차가 있습니다. (API 지연 등)</li>';
                } else {
                    diffContainer.hidden = true;
                }
            }
        } catch (e) {
            console.error('Failed to fetch performance summary', e);
        }

        const trades = await fetchJson('/api/trades?limit=20');
        const tbodyTrades = document.querySelector('#table-trades tbody');
        if (!tbodyTrades) return;
        tbodyTrades.innerHTML = '';

        if (!trades.trades.length) {
            setTableMessage('#table-trades tbody', 8, '주문 기록이 없습니다');
        }

        trades.trades.forEach((trade) => {
            const action = String(trade.action || '').toLowerCase();
            const badge = action === 'buy'
                ? '<span class="badge badge-buy">매수</span>'
                : '<span class="badge badge-sell">매도</span>';
            const [datePart = '-', timePart = '-'] = String(trade.ts || '').split(' ');
            const reason = escapeHtml(translateReason(trade.reason || '-'));
            const orderStatus = orderStatusLabel(trade.order_status);
            const filledQty = Number(trade.filled_qty || 0);
            const filledPrice = Number(trade.filled_price || 0);
            const filledText = filledQty > 0
                ? `${filledQty.toLocaleString()} @ ${formatCurrency(filledPrice)}`
                : '-';

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>
                    <div>${escapeHtml(datePart)}</div>
                    <div class="time-muted">${escapeHtml(timePart.substring(0, 5))}</div>
                </td>
                <td>${badge}</td>
                <td><span class="symbol-name">${escapeHtml(trade.name || trade.symbol)}</span></td>
                <td>${formatCurrency(trade.price)}</td>
                <td>${Number(trade.qty || 0).toLocaleString()}</td>
                <td><div class="reason-cell" title="${reason}">${reason}</div></td>
                <td>
                    <span class="badge">${escapeHtml(orderStatus)}</span>
                    ${trade.broker_order_id ? `<div class="time-muted">#${escapeHtml(trade.broker_order_id)}</div>` : ''}
                </td>
                <td>${escapeHtml(filledText)}</td>
            `;
            tbodyTrades.appendChild(tr);
        });
        
        await renderPeriodicPerformance();
    } catch (err) {
        console.error('Failed to fetch trade history', err);
        setTableMessage('#table-trades tbody', 8, err.message);
    }
}

async function renderPeriodicPerformance() {
    try {
        const periodicData = await fetchJson('/api/performance/periodic', 30000);
        periodicDataCache = periodicData;
        
        // Attach sub-tab event listeners once
        const dailyBtn = document.getElementById('btn-perf-daily');
        const monthlyBtn = document.getElementById('btn-perf-monthly');
        
        if (dailyBtn && !dailyBtn.dataset.listenerAttached) {
            dailyBtn.dataset.listenerAttached = 'true';
            dailyBtn.addEventListener('click', () => {
                periodicActiveTab = 'daily';
                dailyBtn.classList.add('active');
                if (monthlyBtn) monthlyBtn.classList.remove('active');
                updatePeriodicPerformanceUI();
            });
        }
        if (monthlyBtn && !monthlyBtn.dataset.listenerAttached) {
            monthlyBtn.dataset.listenerAttached = 'true';
            monthlyBtn.addEventListener('click', () => {
                periodicActiveTab = 'monthly';
                monthlyBtn.classList.add('active');
                if (dailyBtn) dailyBtn.classList.remove('active');
                updatePeriodicPerformanceUI();
            });
        }
        
        updatePeriodicPerformanceUI();
    } catch (err) {
        console.error('Periodic performance render failed:', err);
    }
}

function updatePeriodicPerformanceUI() {
    if (!periodicDataCache) return;
    setPerformanceDetailPanelOpen(false);
    
    const dataList = periodicActiveTab === 'daily' ? (periodicDataCache.daily || []) : (periodicDataCache.monthly || []);
    
    // 1. Populate the table
    const tbody = document.querySelector('#table-periodic-performance tbody');
    if (tbody) {
        tbody.innerHTML = '';
        if (!dataList.length) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem; color: #94a3b8;">성과 분석 데이터가 없습니다.</td></tr>`;
        } else {
            // Sort to display latest data first in the table
            const tableDataList = [...dataList].reverse();
            tableDataList.forEach(item => {
                const tr = document.createElement('tr');
                const pnl = item.realized_pnl || 0;
                const pnlRate = item.realized_pnl_rate || 0;
                const pnlClass = pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '');
                
                tr.innerHTML = `
                    <td><strong>${escapeHtml(item.period)}</strong></td>
                    <td>${Number(item.order_count || 0).toLocaleString()}회</td>
                    <td>${formatCurrency(item.buy_amount)}</td>
                    <td>${formatCurrency(item.sell_amount)}</td>
                    <td class="${pnlClass}">${pnl > 0 ? '+' : ''}${formatCurrency(pnl)}</td>
                    <td class="${pnlClass}">${pnlRate > 0 ? '+' : ''}${pnlRate.toFixed(2)}%</td>
                    <td class="${pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '')}">${formatCurrency(item.net_cashflow)}</td>
                `;
                const periodCell = tr.querySelector('td');
                if (periodCell) {
                    periodCell.innerHTML = '';
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = 'period-detail-button';
                    button.innerHTML = `<strong>${escapeHtml(item.period)}</strong>`;
                    button.addEventListener('click', () => renderPerformanceDetailPanel(item));
                    periodCell.appendChild(button);
                }
                if (tr.cells[1]) {
                    tr.cells[1].textContent = `${Number(item.order_count || 0).toLocaleString()}건`;
                }
                tbody.appendChild(tr);
            });
        }
    }
    
    // 2. Render Chart.js with defense
    if (typeof Chart === 'undefined') {
        console.warn('Chart.js is not loaded yet.');
        return;
    }
    
    const canvas = document.getElementById('periodicPerformanceChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    if (periodicChartInstance) {
        try {
            periodicChartInstance.destroy();
        } catch (e) {
            console.error('Failed to destroy previous chart instance', e);
        }
        periodicChartInstance = null;
    }
    
    if (!dataList || dataList.length === 0) {
        return;
    }
    
    const labels = dataList.map(item => item.period);
    const pnlData = dataList.map(item => item.realized_pnl || 0);
    const pnlRateData = dataList.map(item => item.realized_pnl_rate || 0);
    
    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = "'Noto Sans KR', 'Inter', sans-serif";
    
    // Dynamic bar colors based on profit/loss
    const barColors = pnlData.map(val => val >= 0 ? 'rgba(34, 197, 94, 0.2)' : 'rgba(239, 68, 68, 0.2)');
    const borderColors = pnlData.map(val => val >= 0 ? 'rgba(34, 197, 94, 0.8)' : 'rgba(239, 68, 68, 0.8)');
    
    try {
        periodicChartInstance = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [
                    {
                        label: '실현손익 (원)',
                        data: pnlData,
                        backgroundColor: barColors,
                        borderColor: borderColors,
                        borderWidth: 1,
                        yAxisID: 'y1',
                        borderRadius: 4
                    },
                    {
                        label: '실현수익률 (%)',
                        data: pnlRateData,
                        type: 'line',
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        borderWidth: 2,
                        pointBackgroundColor: '#3b82f6',
                        pointBorderColor: '#ffffff',
                        pointRadius: 4,
                        pointHoverRadius: 6,
                        tension: 0.3,
                        yAxisID: 'y2'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { boxWidth: 12, color: '#f8fafc' }
                    },
                    tooltip: {
                        padding: 12,
                        callbacks: {
                            label: function(context) {
                                let label = context.dataset.label || '';
                                if (label) {
                                    label += ': ';
                                }
                                if (context.datasetIndex === 0) {
                                    label += formatCurrency(context.parsed.y);
                                } else {
                                    label += (context.parsed.y > 0 ? '+' : '') + Number(context.parsed.y || 0).toFixed(2) + '%';
                                }
                                return label;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#94a3b8' }
                    },
                    y1: {
                        type: 'linear',
                        position: 'left',
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: {
                            color: '#94a3b8',
                            callback: function(value) {
                                const val = Number(value);
                                if (isNaN(val)) return '0';
                                if (val >= 10000 || val <= -10000) {
                                    return (val / 10000).toFixed(0) + '만';
                                }
                                return val.toLocaleString();
                            }
                        },
                        title: { display: true, text: '실현손익 (원)', color: '#22c55e' }
                    },
                    y2: {
                        type: 'linear',
                        position: 'right',
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#94a3b8',
                            callback: function(value) {
                                const val = Number(value);
                                return (isNaN(val) ? 0 : val).toFixed(1) + '%';
                            }
                        },
                        title: { display: true, text: '실현수익률 (%)', color: '#3b82f6' }
                    }
                }
            }
        });
    } catch (chartErr) {
        console.error('Chart initialization failed:', chartErr);
    }
}

async function renderExecutionPlan() {
    const btn = document.getElementById('btn-execution-plan');
    setButtonBusy(btn, true);
    setTableMessage('#table-execution-plan tbody', 8, '실행 계획 불러오는 중...');
    try {
        const strategyId = getActiveStrategyId();
        const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
        const data = await fetchJson(`/api/execution-plan${query}`);
        const plan = data.plan || [];

        const summaryEl = document.getElementById('execution-plan-summary');
        if (summaryEl) {
            const haltBadge = data.daily_loss_halt
                ? ' <span class="badge badge-sell">손실한도 초과 — 신규매수 중단</span>'
                : '';
            summaryEl.innerHTML =
                `<span>모드: <strong>${escapeHtml(data.mode || 'live')}</strong></span>` +
                ` <span>예수금: <strong>${formatCurrency(data.cash)}</strong></span>` +
                ` <span>잔여예수금: <strong>${formatCurrency(data.remaining_cash)}</strong></span>` +
                ` <span>스캔: <strong>${data.scanned || 0}종목</strong></span>` +
                haltBadge;
        }

        const tbody = document.querySelector('#table-execution-plan tbody');
        if (!tbody) return;
        tbody.innerHTML = '';

        if (!plan.length) {
            setTableMessage('#table-execution-plan tbody', 8, '실행 계획이 없습니다');
            return;
        }

        plan.forEach((row) => {
            const action = String(row.action || '').toLowerCase();
            const actionBadge = action === 'buy'
                ? pill('매수', 'buy')
                : action === 'sell'
                ? pill('매도', 'sell')
                : pill('보유', 'hold');

            const decision = row.decision || '';
            const decisionBadge = decision === 'execute'
                ? pill('실행', 'buy')
                : decision === 'queue'
                ? pill('대기', 'warn')
                : decision === 'failed'
                ? pill('실패', 'sell')
                : decision === 'hold'
                ? pill('보유', 'hold')
                : decision
                ? pill(decision, 'hold')
                : '-';

            const reason = escapeHtml(translateReason(row.reason || '-'));
            const estimated = row.estimated_cost || (row.qty && row.price ? row.qty * row.price : 0);

            const queueBtn = decision === 'queue'
                ? '<span class="time-muted">대기중</span>'
                : `<button type="button" class="queue-order button-ghost"
                    data-symbol="${escapeHtml(row.symbol)}"
                    data-name="${escapeHtml(row.name || row.symbol)}"
                    data-action="${escapeHtml(row.action)}"
                    data-qty="${row.qty}"
                    data-price="${row.price}"
                    data-reason="${escapeHtml(row.reason || '')}"
                    data-source="execution_plan"
                    style="padding:3px 8px;font-size:0.75rem;">승인큐</button>`;

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><span class="symbol-name">${escapeHtml(row.name || row.symbol)}</span></td>
                <td>${actionBadge}</td>
                <td>${Number(row.qty || 0).toLocaleString()}</td>
                <td>${formatCurrency(row.price)}</td>
                <td>${formatCurrency(estimated)}</td>
                <td><div class="reason-cell" title="${reason}">${reason}</div></td>
                <td>${decisionBadge}</td>
                <td>${queueBtn}</td>
            `;
            tbody.appendChild(tr);
        });
        bindQueueButtons();
    } catch (err) {
        setTableMessage('#table-execution-plan tbody', 8, err.message);
    } finally {
        setButtonBusy(btn, false);
    }
}

async function fetchDashboardData() {
    try {
        await renderConfig();
    } catch (err) {
        console.error("Failed to load config:", err);
    }
    // Watchlist and other strategy-scoped requests must run after the server-selected
    // strategy has populated the dropdown. Otherwise the template's placeholder or
    // a stale browser value can incorrectly request an empty isolated watchlist.
    await syncStrategiesToDropdown();
    await Promise.all([
        renderRuntime(),
        renderBalance(),
        renderTrades(),
        renderApprovals(),
        renderCandidateHistory(),
        renderStrategyContext(),
        renderAiStrategies(),
        renderWatchlist()
    ]);
}

// 매수후보 포착 히스토리 새로고침 버튼 바인딩
document.addEventListener('DOMContentLoaded', () => {
    const histRefreshBtn = document.getElementById('btn-candidates-history-refresh');
    if (histRefreshBtn) {
        histRefreshBtn.addEventListener('click', async () => {
            setButtonBusy(histRefreshBtn, true);
            await renderCandidateHistory();
            setButtonBusy(histRefreshBtn, false);
        });
    }

    const aiRefreshBtn = document.getElementById('btn-refresh-ai-strategies');
    if (aiRefreshBtn) {
        aiRefreshBtn.addEventListener('click', async () => {
            setButtonBusy(aiRefreshBtn, true);
            await Promise.all([syncStrategiesToDropdown(), renderStrategyContext(), renderAiStrategies()]);
            setButtonBusy(aiRefreshBtn, false);
        });
    }
    document.querySelectorAll('.easy-strategy-preset').forEach((button) => {
        button.addEventListener('click', async () => {
            const preset = button.getAttribute('data-preset');
            setButtonBusy(button, true);
            try {
                const result = await postJson(`/api/ai-strategy-presets/${encodeURIComponent(preset)}/apply`, {});
                const strategyId = result.strategy?.id;
                if (strategyId) {
                    localStorage.setItem('hanstock_ai_ranker', strategyId);
                    activeStrategyAuditId = strategyId;
                }
                await Promise.all([renderAiStrategies(), syncStrategiesToDropdown(), renderStrategyContext()]);
                await renderStrategyAudit(strategyId);
                setStatus(result.message || '쉬운 전략을 적용했습니다.', true);
            } catch (err) {
                setStatus(`쉬운 전략 적용 실패: ${err.message}`);
            } finally {
                setButtonBusy(button, false);
            }
        });
    });
    const advancedStrategyBtn = document.getElementById('btn-toggle-advanced-strategy');
    if (advancedStrategyBtn) {
        advancedStrategyBtn.addEventListener('click', () => {
            const panel = document.querySelector('.panel-add-ai-strategy');
            if (!panel) return;
            panel.hidden = !panel.hidden;
            if (!panel.hidden) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }
    const auditRefreshBtn = document.getElementById('btn-refresh-strategy-audit');
    if (auditRefreshBtn) {
        auditRefreshBtn.addEventListener('click', async () => {
            setButtonBusy(auditRefreshBtn, true);
            await renderStrategyAudit();
            setButtonBusy(auditRefreshBtn, false);
        });
    }

    const addAiForm = document.getElementById('form-add-ai-strategy');
    if (addAiForm) {
        addAiForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const submitBtn = addAiForm.querySelector('button[type="submit"]');
            setButtonBusy(submitBtn, true);

            const formData = new FormData(addAiForm);
            const payload = {
                name: formData.get('strat_name'),
                model: formData.get('strat_model'),
                weight: parseFloat(formData.get('strat_weight')),
                description: formData.get('strat_desc') || '',
                profile: {
                    min_rule_score_for_ai: parseFloat(formData.get('strat_min_rule_score') || '1.5'),
                    min_ai_confidence: parseFloat(formData.get('strat_min_confidence') || '0.6'),
                    allow_candidate_promotion: formData.get('strat_allow_promotion') === 'true'
                }
            };

            try {
                await postJson('/api/ai-strategies', payload);
                setStatus('신규 AI 전략이 성공적으로 등록되었습니다.', true);
                addAiForm.reset();
                const weightInput = addAiForm.querySelector('input[name="strat_weight"]');
                if (weightInput) weightInput.value = "0.4";
                const ruleInput = addAiForm.querySelector('input[name="strat_min_rule_score"]');
                if (ruleInput) ruleInput.value = "1.5";
                const confInput = addAiForm.querySelector('input[name="strat_min_confidence"]');
                if (confInput) confInput.value = "0.6";
                const promoInput = addAiForm.querySelector('select[name="strat_allow_promotion"]');
                if (promoInput) promoInput.value = "false";
                
                await Promise.all([renderAiStrategies(), syncStrategiesToDropdown(), renderStrategyContext()]);
            } catch (err) {
                setStatus(`전략 추가 실패: ${err.message}`);
            } finally {
                setButtonBusy(submitBtn, false);
            }
        });
    }

    const applySelectedBtn = document.getElementById('btn-apply-selected-strategies');
    if (applySelectedBtn) {
        applySelectedBtn.addEventListener('click', async () => {
            setButtonBusy(applySelectedBtn, true);
            try {
                await Promise.all([renderAiStrategies(), syncStrategiesToDropdown(), renderStrategyContext()]);
                
                const select = document.getElementById('select-ai-ranker');
                if (select && select.options.length > 0) {
                    const data = await fetchJson('/api/ai-strategies');
                    const activeStrats = data.strategies.filter(s => s.selected);
                    if (activeStrats.length > 0) {
                        select.value = activeStrats[0].id;
                        localStorage.setItem('hanstock_ai_ranker', select.value);
                    }
                }
                
                const strategyTabBtn = document.querySelector('.dashboard-tab[data-dashboard-tab="strategy"]');
                if (strategyTabBtn) {
                    strategyTabBtn.click();
                }
                
                await renderCandidates();
                setStatus('선택한 AI 전략이 대시보드에 실시간 바인딩되어 신규매수후보 찾기가 완료되었습니다.', true);
            } catch (err) {
                setStatus(`전략 자동 적용 실패: ${err.message}`);
            } finally {
                setButtonBusy(applySelectedBtn, false);
            }
        });
    }

    // 관심 종목 수동 추가 폼 바인딩
    const addWatchlistForm = document.getElementById('form-watchlist-add');
    if (addWatchlistForm) {
        addWatchlistForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const submitBtn = addWatchlistForm.querySelector('button[type="submit"]');
            setButtonBusy(submitBtn, true);
            
            const formData = new FormData(addWatchlistForm);
            const rawVal = formData.get('watchlist_code');
            const symbol = rawVal.trim ? rawVal.trim() : rawVal;
            
            try {
                const strategyId = getActiveStrategyId();
                const res = await postJson('/api/watchlist', {
                    symbol: symbol,
                    strategy_id: strategyId || null,
                });
                setStatus(`관심 종목에 성공적으로 추가되었습니다: ${res.name} (${res.symbol})`, true);
                addWatchlistForm.reset();
                await renderWatchlist();
            } catch (err) {
                setStatus(`관심 종목 추가 실패: ${err.message}`);
            } finally {
                setButtonBusy(submitBtn, false);
            }
        });
    }

    // AI 자동 추가 적용 토글 및 임계값 제어 바인딩
    const chkWatchlistAiAuto = document.getElementById('chk-watchlist-ai-auto');
    const numWatchlistAiThreshold = document.getElementById('num-watchlist-ai-threshold');
    
    async function syncWatchlistSettings() {
        if (!chkWatchlistAiAuto) return;
        const checked = chkWatchlistAiAuto.checked;
        const threshold = numWatchlistAiThreshold ? parseFloat(numWatchlistAiThreshold.value) : 3.0;
        try {
            await postJson('/api/watchlist/toggle-auto', { enabled: checked, threshold: threshold });
            setStatus(`AI 자동 관심 종목 추가설정(여부: ${checked ? '활성화' : '비활성화'}, 기준: ${threshold}점)이 반영되었습니다.`, true);
        } catch (err) {
            setStatus(`AI 자동 추가설정 동기화 실패: ${err.message}`);
        }
    }

    if (chkWatchlistAiAuto) {
        chkWatchlistAiAuto.addEventListener('change', syncWatchlistSettings);
    }
    if (numWatchlistAiThreshold) {
        numWatchlistAiThreshold.addEventListener('change', syncWatchlistSettings);
    }

    // AI 자동 즉시 스캔 가동 버튼 바인딩
    const btnWatchlistAiScan = document.getElementById('btn-watchlist-ai-scan');
    if (btnWatchlistAiScan) {
        btnWatchlistAiScan.addEventListener('click', async () => {
            setButtonBusy(btnWatchlistAiScan, true);
            setStatus('AI 자동추가 즉시 스캔이 가동되었습니다. 시장 유니버스를 실시간 탐색 중입니다...', true);
            
            try {
                const threshold = numWatchlistAiThreshold ? parseFloat(numWatchlistAiThreshold.value) : 3.0;
                const enabled = chkWatchlistAiAuto ? chkWatchlistAiAuto.checked : true;
                const res = await postJson('/api/watchlist/scan-trigger', {
                    enabled,
                    threshold
                });
                const usedThreshold = Number(res.threshold_used ?? threshold);
                if (res.added_count > 0) {
                    const names = res.added_symbols.map(s => `${s.name}(${s.symbol})`).join(', ');
                    setStatus(
                        `AI 스캔 완료: ${usedThreshold}점 이상 ${res.eligible_count || 0}개, ` +
                        `기등록 ${res.already_registered_count || 0}개, 신규 추가 ${res.added_count}개: ${names}`,
                        true
                    );
                } else {
                    setStatus(
                        `AI 스캔 완료: 기준 ${usedThreshold}점, 대상 ${res.eligible_count || 0}개, ` +
                        `기등록 ${res.already_registered_count || 0}개, 신규 추가 0개. ` +
                        `기존 관심종목은 기준 변경만으로 자동 삭제되지 않습니다. (분석: ${res.scanned}종목)`,
                        true
                    );
                }
                await renderWatchlist();
            } catch (err) {
                setStatus(`AI 즉시 스캔 실패: ${err.message}`);
            } finally {
                setButtonBusy(btnWatchlistAiScan, false);
            }
        });
    }
    const btnSignals = document.getElementById('btn-signals');
    if (btnSignals) {
        btnSignals.addEventListener('click', renderSignals);
    }

    const btnSyncTrades = document.getElementById('btn-sync-trades');
    if (btnSyncTrades) {
        btnSyncTrades.addEventListener('click', async () => {
            btnSyncTrades.disabled = true;
            btnSyncTrades.textContent = '동기화 중...';
            btnSyncTrades.style.backgroundColor = '#f59e0b'; // warning yellow
            btnSyncTrades.style.color = 'white';
            try {
                const result = await postJson('/api/trades/sync', {});
                setStatus(`증권사 기록 동기화 완료 (누락된 ${result.synced_count}건 추가됨)`, true);
                // 동기화 후 보유 관련 탭들을 함께 현행화한다.
                await Promise.all([
                    renderTrades(),
                    renderBalance(),
                    renderPeriodicPerformance(),
                    renderExecutionPlan(),
                ]);
                
                btnSyncTrades.textContent = result.synced_count > 0 ? `동기화 완료 (${result.synced_count}건)` : '동기화 완료 ✔️';
                btnSyncTrades.style.backgroundColor = '#10b981'; // success green
                btnSyncTrades.style.color = 'white';
                
                setTimeout(() => {
                    btnSyncTrades.disabled = false;
                    btnSyncTrades.textContent = '증권사 기록 동기화';
                    btnSyncTrades.style.backgroundColor = '';
                    btnSyncTrades.style.color = '';
                }, 3000);
                
            } catch (err) {
                setStatus(`동기화 실패: ${err.message}`);
                btnSyncTrades.textContent = '동기화 실패';
                btnSyncTrades.style.backgroundColor = '#ef4444'; // error red
                btnSyncTrades.style.color = 'white';
                
                setTimeout(() => {
                    btnSyncTrades.disabled = false;
                    btnSyncTrades.textContent = '증권사 기록 동기화';
                    btnSyncTrades.style.backgroundColor = '';
                    btnSyncTrades.style.color = '';
                }, 3000);
            }
        });
    }

    const btnCandidates = document.getElementById('btn-candidates');
    if (btnCandidates) {
        btnCandidates.addEventListener('click', renderCandidates);
    }
    const btnExecutionPlan = document.getElementById('btn-execution-plan');
    if (btnExecutionPlan) {
        btnExecutionPlan.addEventListener('click', renderExecutionPlan);
    }
    const btnApprovals = document.getElementById('btn-approvals');
    if (btnApprovals) {
        btnApprovals.addEventListener('click', renderApprovals);
    }
    const btnAiAllocation = document.getElementById('btn-ai-allocation');
    if (btnAiAllocation) {
        btnAiAllocation.addEventListener('click', renderAiAllocation);
    }
    const btnOptimizer = document.getElementById('btn-optimizer');
    if (btnOptimizer) {
        btnOptimizer.addEventListener('click', renderOptimizer);
    }
    const btnOptimizerBatch = document.getElementById('btn-optimizer-batch');
    if (btnOptimizerBatch) {
        btnOptimizerBatch.addEventListener('click', processOptimizerBatch);
    }
    const btnAutoApproval = document.getElementById('btn-auto-approval');
    if (btnAutoApproval) {
        btnAutoApproval.addEventListener('click', toggleAutoApproval);
    }
    const btnSellAllHoldings = document.getElementById('btn-sell-all-holdings');
    if (btnSellAllHoldings) {
        btnSellAllHoldings.addEventListener('click', sellAllHoldings);
    }
    const btnDryRun = document.getElementById('btn-dry-run');
    if (btnDryRun) {
        btnDryRun.addEventListener('click', () => toggleRuntimeOrderMode('btn-dry-run', 'DRY_RUN', '주문차단'));
    }

    setTableMessage('#table-signals tbody', 7, '진단하기를 누르면 보유 종목 신호를 확인합니다');
    setTableMessage('#table-candidates tbody', 9, '찾기를 누르면 관심종목에서 매수 후보를 검색합니다');
    setTableMessage('#table-execution-plan tbody', 8, '불러오기를 누르면 다음 사이클 실행 계획을 표시합니다');
    setTableMessage('#table-approvals tbody', 9, '승인 대기 주문이 없습니다');
    setTableMessage('#table-ai-allocation tbody', 8, '계산을 누르면 AI 목표 비중을 확인합니다');
    setTableMessage('#table-optimizer tbody', 7, '최적화를 누르면 리스크 기반 목표 비중을 확인합니다');
    
    fetchDashboardData();
    
    setInterval(() => Promise.all([
        renderRuntime(),
        renderBalance(),
        renderTrades(),
        renderApprovals(),
        renderCandidateHistory(),
        syncStrategiesToDropdown(),
        renderAiStrategies(),
        renderWatchlist()
    ]).catch(err => console.error("Polling error:", err)), 30000);
});

window.showAiModal = function(element) {
    const payloadText = element.getAttribute('data-ai-payload');
    const titleEl = document.getElementById('aiModalTitle');
    const subtitleEl = document.getElementById('aiModalSubtitle');
    const bodyEl = document.getElementById('aiModalBody');

    if (!titleEl || !bodyEl) {
        return;
    }

    if (payloadText) {
        try {
            const payload = JSON.parse(decodeURIComponent(payloadText));
            titleEl.textContent = `${payload.name || payload.symbol || 'AI 전략'} 상세 근거`;
            if (subtitleEl) {
                subtitleEl.textContent = payload.ai_strategy_name || '';
            }
            bodyEl.innerHTML = buildAiModalMarkup(payload);
        } catch (_err) {
            const reasonText = element.getAttribute('data-reason') || '-';
            titleEl.textContent = 'AI 전략 상세 근거';
            if (subtitleEl) {
                subtitleEl.textContent = '';
            }
            bodyEl.textContent = reasonText;
        }
    } else {
        const reasonText = element.getAttribute('data-reason') || '-';
        titleEl.textContent = 'AI 전략 상세 근거';
        if (subtitleEl) {
            subtitleEl.textContent = '';
        }
        bodyEl.textContent = reasonText;
    }
    setAiModalOpen(true);
};

window.addEventListener('load', () => {
    const aiModal = document.getElementById('aiModal');
    const ncModal = document.getElementById('noCandidatesModal');
    const closePerformanceDetailBtn = document.getElementById('btn-close-performance-detail');

    // 닫기 버튼 — 모든 .close-modal 버튼을 각 모달 컨텍스트로 연결
    document.querySelectorAll('.close-modal').forEach(btn => {
        btn.addEventListener('click', () => {
            setAiModalOpen(false);
            setNoCandidatesModalOpen(false);
        });
    });
    if (closePerformanceDetailBtn) {
        closePerformanceDetailBtn.addEventListener('click', () => setPerformanceDetailPanelOpen(false));
    }

    window.addEventListener('click', (event) => {
        if (event.target === aiModal) setAiModalOpen(false);
        if (event.target === ncModal) setNoCandidatesModalOpen(false);
    });

    window.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            setAiModalOpen(false);
            setNoCandidatesModalOpen(false);
        }
    });

    // AI 전략 컨트롤 드롭다운 초기화 및 바인딩
    const rankerSelect = document.getElementById('select-ai-ranker');
    const optimizerSelect = document.getElementById('select-portfolio-optimizer');
    const applyBtn = document.getElementById('btn-apply-strategy');
    
    if (rankerSelect) {
        const savedRanker = localStorage.getItem('hanstock_ai_ranker');
        if (savedRanker) rankerSelect.value = savedRanker;
        rankerSelect.addEventListener('change', async () => {
            localStorage.setItem('hanstock_ai_ranker', rankerSelect.value);
            await postJson(`/api/ai-strategies/${encodeURIComponent(rankerSelect.value)}/select`, {
                selected: true,
            });
            await Promise.all([
                renderStrategyContext(),
                renderWatchlist(),
                renderSignals(),
                renderExecutionPlan(),
            ]);
        });
    }
    
    if (optimizerSelect) {
        const savedOptimizer = localStorage.getItem('hanstock_portfolio_optimizer');
        if (savedOptimizer) optimizerSelect.value = savedOptimizer;
        optimizerSelect.addEventListener('change', () => {
            localStorage.setItem('hanstock_portfolio_optimizer', optimizerSelect.value);
        });
    }
    
    if (applyBtn) {
        applyBtn.addEventListener('click', renderCandidates);
    }


    // ----------------------------------------------------
    // Scheduler Tab Manual Run Buttons Binding
    // ----------------------------------------------------
    const btnRunDailyAuto = document.getElementById('btn-run-daily-auto');
    const btnRunAnalysisOnly = document.getElementById('btn-run-analysis-only');
    const btnRunExecute = document.getElementById('btn-run-execute');

    if (btnRunDailyAuto) {
        btnRunDailyAuto.addEventListener('click', () => triggerSchedule('daily_auto'));
    }
    if (btnRunAnalysisOnly) {
        btnRunAnalysisOnly.addEventListener('click', () => triggerSchedule('analysis_only'));
    }
    if (btnRunExecute) {
        btnRunExecute.addEventListener('click', () => triggerSchedule('execute'));
    }

    // Load initial schedule info
    if (typeof renderScheduleInfo === 'function') {
        renderScheduleInfo();
    }
});

// ----------------------------------------------------
// Scheduler Tab Rendering & Operation Helpers
// ----------------------------------------------------

async function renderScheduleInfo() {
    try {
        const strategyId = getActiveStrategyId();
        const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
        const data = await fetchJson(`/api/scheduler/status${query}`);
        await renderSchedulerStrategyChecklist(data.strategy_dispatch?.schedules || []);
        
        // 1. Config / Settings
        const cronTzEl = document.getElementById('sched-cron-tz');
        if (cronTzEl) cronTzEl.textContent = data.config.cron_tz || '-';
        
        const dailyRetriesEl = document.getElementById('sched-daily-retries');
        if (dailyRetriesEl) dailyRetriesEl.textContent = `${data.config.daily_auto_retries}회`;
        
        const dailyRetryDelayEl = document.getElementById('sched-daily-retry-delay');
        if (dailyRetryDelayEl) dailyRetryDelayEl.textContent = `${data.config.daily_auto_retry_delay_seconds}초`;
        
        const retriesEl = document.getElementById('sched-retries');
        if (retriesEl) retriesEl.textContent = `${data.config.scheduler_retries}회`;
        
        const retryDelayEl = document.getElementById('sched-retry-delay');
        if (retryDelayEl) retryDelayEl.textContent = `${data.config.scheduler_retry_delay_seconds}초`;
        
        const slackEnabledEl = document.getElementById('sched-slack-enabled');
        if (slackEnabledEl) slackEnabledEl.textContent = data.config.slack_enabled === 'true' ? '활성화' : '비활성화';
        
        const syncEnabledEl = document.getElementById('sched-sync-enabled');
        if (syncEnabledEl) syncEnabledEl.textContent = data.config.sync_enabled === 'true' ? '활성화' : '비활성화';
        
        const tradingEnvEl = document.getElementById('sched-trading-env');
        if (tradingEnvEl) tradingEnvEl.textContent = data.config.trading_env === 'real' ? '실전투자' : '모의투자';

        const activeStrategyEl = document.getElementById('sched-active-strategy');
        if (activeStrategyEl) {
            const dispatch = data.strategy_dispatch || {};
            const dispatchText = dispatch.summary || `사용 ${dispatch.enabled_count || 0}개 / 전체 ${dispatch.schedule_count || 0}개 / 감시종목 ${dispatch.universe_count || 0}개`;
            activeStrategyEl.textContent = `${data.active_strategy_name || '-'} (${data.active_strategy_id || '-'}) · ${dispatchText}`;
        }
        
        // 2. Dynamic status of current/last execution state
        const runState = data.run_state;
        const runningPanel = document.getElementById('scheduler-running-panel');
        if (runningPanel) {
            if (runState.is_running) {
                runningPanel.style.display = 'block';
                startSchedulerPolling(runState.mode);
            } else {
                runningPanel.style.display = 'none';
                if (schedulerPollInterval) {
                    clearInterval(schedulerPollInterval);
                    schedulerPollInterval = null;
                }
                // Enable trigger buttons
                disableTriggerButtons(false);
            }
        }
        
        // 3. Render last result
        const lastResult = data.last_result;
        if (lastResult) {
            const timeEl = document.getElementById('sched-last-run-time');
            if (timeEl) {
                const summaryLabel = lastResult.summary_label || '최근 실행';
                timeEl.textContent = `${summaryLabel} · 최종 실행: ${formatKstTime(lastResult.recorded_at)}`;
            }
            
            const results = lastResult.result.results || [];
            const approved = lastResult.result.auto_approved || [];
            const approvalErrors = lastResult.result.auto_approval_errors || [];
            const runErrors = lastResult.result.errors || lastResult.result.retry_errors || [];
            const summaryCounts = lastResult.result.summary_counts || {};
            
            // Update daily total summary metrics at the top
            const totalPlanCount = summaryCounts.plan_count ?? results.length;
            const queuedCreatedCount = results.filter(r => r.decision === 'queue').length;
            const totalQueuedCount = summaryCounts.queue_count ?? Math.max(0, queuedCreatedCount - approved.length - approvalErrors.length);
            const totalApprovedCount = summaryCounts.approved_count ?? approved.filter(a => a.status === 'executed').length;
            const totalFailedCount = summaryCounts.failed_count ?? approved.filter(a => a.status === 'failed').length + approvalErrors.length + runErrors.length;
            
            const planCntEl = document.getElementById('sched-result-plan-cnt');
            if (planCntEl) planCntEl.textContent = `${totalPlanCount}건`;
            
            const queueCntEl = document.getElementById('sched-result-queue-cnt');
            if (queueCntEl) queueCntEl.textContent = `${totalQueuedCount}건`;
            
            const approvedCntEl = document.getElementById('sched-result-approved-cnt');
            if (approvedCntEl) approvedCntEl.textContent = `${totalApprovedCount}건`;
            
            const failedCntEl = document.getElementById('sched-result-failed-cnt');
            if (failedCntEl) failedCntEl.textContent = `${totalFailedCount}건`;
            
            // Update Daily Status Badge at the top
            const totalFailed = totalFailedCount > 0;
            const statusEl = document.getElementById('sched-result-status');
            if (statusEl) {
                statusEl.textContent = totalFailed ? '오류 발생' : '정상 완료';
                statusEl.className = totalFailed ? 'badge badge-danger' : 'badge badge-success';
                statusEl.style.color = totalFailed ? 'var(--danger)' : 'var(--success)';
            }
            
            // Build groups dynamically by round
            const uniqueRounds = new Map(); // round -> { time, results, approved, approvalErrors, mode }
            results.forEach(r => {
                if (r.round) {
                    if (!uniqueRounds.has(r.round)) {
                        uniqueRounds.set(r.round, { time: r.time || '', results: [], approved: [], approvalErrors: [], mode: lastResult.mode });
                    }
                    uniqueRounds.get(r.round).results.push(r);
                }
            });
            approved.forEach(a => {
                if (a.round) {
                    if (!uniqueRounds.has(a.round)) {
                        uniqueRounds.set(a.round, { time: a.time || '', results: [], approved: [], approvalErrors: [], mode: lastResult.mode });
                    }
                    uniqueRounds.get(a.round).approved.push(a);
                }
            });
            approvalErrors.forEach(e => {
                if (e.round) {
                    if (!uniqueRounds.has(e.round)) {
                        uniqueRounds.set(e.round, { time: e.time || '', results: [], approved: [], approvalErrors: [], mode: lastResult.mode });
                    }
                    uniqueRounds.get(e.round).approvalErrors.push(e);
                }
            });
            
            // If no rounds were parsed (e.g. single fallback run), group under Round 1
            if (uniqueRounds.size === 0 && (results.length > 0 || approved.length > 0 || approvalErrors.length > 0)) {
                uniqueRounds.set(1, {
                    time: lastResult.recorded_at ? lastResult.recorded_at.replace("T", " ").split(" ")[1]?.substring(0, 5) || '-' : '-',
                    results: results,
                    approved: approved,
                    approvalErrors: approvalErrors,
                    mode: lastResult.mode
                });
            }
            
            // Initialize expanded rounds set if not exists
            const sortedRoundIds = Array.from(uniqueRounds.keys()).sort((a, b) => b - a); // DESC: latest at top
            if (!window._expandedRounds) {
                window._expandedRounds = new Set();
                if (sortedRoundIds.length > 0) {
                    window._expandedRounds.add(sortedRoundIds[0]); // Expand latest round by default
                }
            }
            
            // Build collapsible rounds container HTML
            const runsContainer = document.getElementById('scheduler-runs-container');
            if (runsContainer) {
                runsContainer.innerHTML = '';
                if (uniqueRounds.size === 0) {
                    runsContainer.innerHTML = `
                        <div class="text-center" style="color: var(--text-muted); font-size: 0.95rem; padding: 3rem 0;">
                            생성된 실행 계획 및 결과 내역이 없습니다.
                        </div>`;
                } else {
                    sortedRoundIds.forEach(round => {
                        const roundData = uniqueRounds.get(round);
                        const isExpanded = window._expandedRounds.has(round);
                        const planCount = roundData.results.length;
                        const approvedCount = roundData.approved.filter(a => a.status === 'executed').length;
                        const failedCount = roundData.approved.filter(a => a.status === 'failed').length + roundData.approvalErrors.length;
                        const hasFailure = failedCount > 0;
                        const timeVal = roundData.time || '-';
                        const modeKor = roundData.mode === 'daily_auto' ? 'AI 자동매매' : (roundData.mode === 'execute' ? '주문 실행' : '분석 전용');
                        
                        // Create card element
                        const card = document.createElement('div');
                        card.className = 'card glass scheduler-round-card';
                        card.style.cssText = 'margin-bottom: 1.25rem; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--bg-card); box-shadow: 0 4px 15px rgba(0,0,0,0.15);';
                        
                        card.innerHTML = `
                            <!-- Card Header -->
                            <div class="round-header" 
                                 style="padding: 1rem 1.25rem; display: flex; justify-content: space-between; align-items: center; cursor: pointer; background: rgba(255, 255, 255, 0.02); transition: background 0.2s;" 
                                 onclick="toggleRoundCollapse(${round})" 
                                 onmouseover="this.style.background='rgba(255, 255, 255, 0.05)'" 
                                 onmouseout="this.style.background='rgba(255, 255, 255, 0.02)'">
                                <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
                                    <span class="badge" style="background: var(--primary); color: #fff; padding: 0.25rem 0.5rem; font-size: 0.8rem; font-weight: 600; border-radius: 4px;">${round}차 실행</span>
                                    <span style="font-weight: 500; font-size: 0.95rem; color: var(--text); display: flex; align-items: center; gap: 0.25rem;">
                                        <i class="far fa-clock" style="font-size: 0.85rem; color: var(--text-muted);"></i> ${timeVal}
                                    </span>
                                    <span class="badge" style="background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text-muted); font-size: 0.75rem; padding: 0.15rem 0.4rem; border-radius: 4px;">${modeKor}</span>
                                </div>
                                <div style="display: flex; align-items: center; gap: 1rem;">
                                    <span style="font-size: 0.85rem; color: var(--text-muted);" class="d-none d-sm-inline">
                                        계획 <strong style="color: var(--text);">${planCount}</strong>건 | 
                                        승인 <strong style="color: var(--success);">${approvedCount}</strong>건 | 
                                        실패 <strong style="color: var(--danger);">${failedCount}</strong>건
                                    </span>
                                    <span class="badge" style="background: ${hasFailure ? 'rgba(var(--danger-rgb, 220, 53, 69), 0.1)' : 'rgba(var(--success-rgb, 40, 167, 69), 0.1)'}; color: ${hasFailure ? 'var(--danger)' : 'var(--success)'}; border: 1px solid ${hasFailure ? 'rgba(var(--danger-rgb), 0.2)' : 'rgba(var(--success-rgb), 0.2)'}; font-size: 0.8rem; padding: 0.2rem 0.5rem; border-radius: 4px;">
                                        ${hasFailure ? '오류 발생' : '정상 완료'}
                                    </span>
                                    <i class="fas fa-chevron-down toggle-icon" id="toggle-icon-${round}" style="transition: transform 0.2s; color: var(--text-muted); transform: ${isExpanded ? 'rotate(180deg)' : 'rotate(0deg)'};"></i>
                                </div>
                            </div>
                            
                            <!-- Card Body -->
                            <div class="round-body" id="round-body-${round}" style="display: ${isExpanded ? 'block' : 'none'}; padding: 1.25rem; border-top: 1px solid var(--border); background: rgba(0, 0, 0, 0.05);">
                                <h4 style="margin-bottom: 0.75rem; font-size: 0.95rem; font-weight: 500; display: flex; align-items: center; gap: 0.5rem; color: var(--text);">
                                    <span style="width: 4px; height: 14px; background: var(--success); display: inline-block; border-radius: 2px;"></span>
                                    자동 승인 및 주문 전송 내역
                                </h4>
                                <div class="table-responsive" style="margin-bottom: 1.5rem; border-radius: 6px; border: 1px solid var(--border); overflow: hidden;">
                                    <table class="table-orders" style="width: 100%; border-collapse: collapse;">
                                        <thead>
                                            <tr style="background: rgba(255,255,255,0.02); border-bottom: 1px solid var(--border);">
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 150px;">주문ID</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 100px;">종목코드</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 120px;">종목명</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 130px;">전략</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 80px;">구분</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: right; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 80px;">수량</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: right; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 120px;">가격</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 100px;">상태</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted);">응답 메세지</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <!-- orders rows go here -->
                                        </tbody>
                                    </table>
                                </div>

                                <h4 style="margin-bottom: 0.75rem; font-size: 0.95rem; font-weight: 500; display: flex; align-items: center; gap: 0.5rem; color: var(--text);">
                                    <span style="width: 4px; height: 14px; background: var(--primary); display: inline-block; border-radius: 2px;"></span>
                                    생성된 매매 계획 및 판단
                                </h4>
                                <div class="table-responsive" style="border-radius: 6px; border: 1px solid var(--border); overflow: hidden;">
                                    <table class="table-plans" style="width: 100%; border-collapse: collapse;">
                                        <thead>
                                            <tr style="background: rgba(255,255,255,0.02); border-bottom: 1px solid var(--border);">
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 100px;">종목코드</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted);">종목명</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 130px;">전략</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 100px;">분류</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 100px;">결정</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: right; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 80px;">수량</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: right; font-size: 0.85rem; font-weight: 500; color: var(--text-muted); width: 120px;">가격</th>
                                                <th style="padding: 0.6rem 0.75rem; text-align: left; font-size: 0.85rem; font-weight: 500; color: var(--text-muted);">근거</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            <!-- plans rows go here -->
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        `;
                        
                        // Populate Plans table inside this round body
                        const plansTbody = card.querySelector('.table-plans tbody');
                        if (plansTbody) {
                            if (roundData.results.length === 0) {
                                plansTbody.innerHTML = '<tr><td colspan="8" class="text-center" style="padding: 1.5rem; font-size: 0.9rem; color: var(--text-muted);">생성된 계획이 없습니다.</td></tr>';
                            } else {
                                roundData.results.forEach(row => {
                                    const tr = document.createElement('tr');
                                    tr.style.borderBottom = '1px solid var(--border)';
                                    const decision = row.decision || (row.approval_id ? 'approved' : 'skip');
                                    const kind = decision === 'execute' || decision === 'approved' ? 'buy' : (decision === 'skip' ? 'hold' : 'warn');
                                    
                                    const displayReason = schedulerReasonText(row);
                                    
                                    tr.innerHTML = `
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${escapeHtml(row.symbol || '-')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="symbol-name" style="font-weight: 500;">${escapeHtml(row.name || '-')}</div></td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(row.strategy_name || row.strategy_id || '기본 분할매매', 'hold')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(toKorPlanCategory(row.category), 'hold')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(schedulerDecisionLabel(decision, row), kind)}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right;">${formatNumber(row.qty || row.signal_qty)}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right; font-weight: 500;">${formatNumber(row.price || row.signal_price)} 원</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="reason-cell" style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(displayReason)}">${escapeHtml(displayReason)}</div></td>
                                    `;
                                    plansTbody.appendChild(tr);
                                });
                            }
                        }
                        
                        // Populate Orders table inside this round body
                        const ordersTbody = card.querySelector('.table-orders tbody');
                        if (ordersTbody) {
                            if (roundData.approved.length === 0 && roundData.approvalErrors.length === 0) {
                                ordersTbody.innerHTML = '<tr><td colspan="9" class="text-center" style="padding: 1.5rem; font-size: 0.9rem; color: var(--text-muted);">승인 대기 주문이 없거나 자동 승인이 생략되었습니다.</td></tr>';
                            } else {
                                // 1. Render approvalErrors first so they appear at the very top
                                roundData.approvalErrors.forEach(err => {
                                    const tr = document.createElement('tr');
                                    tr.style.borderBottom = '1px solid var(--border)';
                                    let cleanMsg = err.message || '오류 발생';
                                    if (cleanMsg.startsWith('[')) {
                                        const closingIdx = cleanMsg.indexOf(']');
                                        if (closingIdx !== -1) {
                                            cleanMsg = cleanMsg.substring(closingIdx + 1).trim();
                                        }
                                    }
                                    
                                    // Lookup stock info from generated plans matching the approval_id
                                    const matchingPlan = roundData.results.find(r => r.approval_id && String(r.approval_id) === String(err.approval_id));
                                    const symbolVal = matchingPlan ? matchingPlan.symbol : '-';
                                    const nameVal = matchingPlan ? matchingPlan.name : '-';
                                    const actionVal = matchingPlan ? matchingPlan.action : '-';
                                    const qtyVal = matchingPlan ? (matchingPlan.qty || matchingPlan.signal_qty) : '-';
                                    const priceVal = matchingPlan ? (matchingPlan.price || matchingPlan.signal_price) : '-';
                                    
                                    tr.innerHTML = `
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${escapeHtml(err.approval_id || '-')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${escapeHtml(symbolVal)}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="symbol-name" style="font-weight: 500;">${escapeHtml(nameVal)}</div></td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(err.strategy_name || (matchingPlan && matchingPlan.strategy_name) || err.strategy_id || (matchingPlan && matchingPlan.strategy_id) || '기본 분할매매', 'hold')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${actionVal !== '-' ? pill(toKorAction(actionVal), actionVal === 'sell' ? 'sell' : 'buy') : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right;">${qtyVal !== '-' ? formatNumber(qtyVal) : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right; font-weight: 500;">${priceVal !== '-' ? formatNumber(priceVal) + ' 원' : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill('승인오류', 'sell')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="reason-cell text-danger" style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(err.message || '')}">${escapeHtml(cleanMsg)}</div></td>
                                    `;
                                    ordersTbody.appendChild(tr);
                                });

                                // 2. Then render normal approved executions (both success and fail)
                                roundData.approved.forEach(ord => {
                                    const tr = document.createElement('tr');
                                    tr.style.borderBottom = '1px solid var(--border)';
                                    const isSuccess = ord.status === 'executed';
                                    let cleanMsg = ord.response_msg || ord.message || '정상 처리';
                                    if (cleanMsg.startsWith('[')) {
                                        const closingIdx = cleanMsg.indexOf(']');
                                        if (closingIdx !== -1) {
                                            cleanMsg = cleanMsg.substring(closingIdx + 1).trim();
                                        }
                                    }
                                    
                                    const ordId = ord.id || ord.approval_id;
                                    // Lookup stock info from generated plans matching the approval_id
                                    const matchingPlan = roundData.results.find(r => r.approval_id && String(r.approval_id) === String(ordId));
                                    const symbolVal = ord.symbol || (matchingPlan ? matchingPlan.symbol : '-');
                                    const nameVal = ord.name || (matchingPlan ? matchingPlan.name : '-');
                                    const actionVal = ord.action || (matchingPlan ? matchingPlan.action : 'buy');
                                    const qtyVal = ord.qty !== undefined && ord.qty !== null ? ord.qty : (matchingPlan ? (matchingPlan.qty || matchingPlan.signal_qty) : '-');
                                    const priceVal = ord.price !== undefined && ord.price !== null ? ord.price : (matchingPlan ? (matchingPlan.price || matchingPlan.signal_price) : '-');
                                    
                                    tr.innerHTML = `
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${escapeHtml(ordId || '-')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${escapeHtml(symbolVal)}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="symbol-name" style="font-weight: 500;">${escapeHtml(nameVal)}</div></td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(ord.strategy_name || (matchingPlan && matchingPlan.strategy_name) || ord.strategy_id || (matchingPlan && matchingPlan.strategy_id) || '기본 분할매매', 'hold')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${actionVal !== '-' ? pill(toKorAction(actionVal), actionVal === 'sell' ? 'sell' : 'buy') : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right;">${qtyVal !== '-' ? formatNumber(qtyVal) : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem; text-align: right; font-weight: 500;">${priceVal !== '-' ? formatNumber(priceVal) + ' 원' : '-'}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;">${pill(isSuccess ? '성공' : '실패', isSuccess ? 'buy' : 'sell')}</td>
                                        <td style="padding: 0.6rem 0.75rem; font-size: 0.85rem;"><div class="reason-cell" style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(ord.response_msg || ord.message || '')}">${escapeHtml(cleanMsg)}</div></td>
                                    `;
                                    ordersTbody.appendChild(tr);
                                });
                            }
                        }
                        
                        runsContainer.appendChild(card);
                    });
                }
            }
        }
    } catch (err) {
        console.error('Failed to load schedule status:', err);
    }
}

async function renderSchedulerStrategyChecklist(schedules = []) {
    const container = document.getElementById('scheduler-strategy-checklist');
    if (!container) return;
    const data = await fetchJson('/api/ai-strategies');
    const scheduled = new Set(schedules.filter((row) => row.enabled).map((row) => String(row.strategy_id)));
    const strategies = (data.strategies || []).filter((row) => row.status !== 'retired');
    container.innerHTML = strategies.map((strategy) => {
        const checked = strategy.selected || scheduled.has(String(strategy.id));
        return `<label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
            <input type="checkbox" class="scheduler-strategy-checkbox" value="${escapeHtml(strategy.id)}" ${checked ? 'checked' : ''}>
            <span>${escapeHtml(strategyDisplayName(strategy))}</span>
        </label>`;
    }).join('') || '<span class="time-muted">실행 가능한 전략이 없습니다.</span>';
}

function getScheduledStrategyIds() {
    return Array.from(document.querySelectorAll('.scheduler-strategy-checkbox:checked'))
        .map((input) => String(input.value || '').trim())
        .filter(Boolean);
}

window.toggleRoundCollapse = function(round) {
    const body = document.getElementById(`round-body-${round}`);
    if (!body) return;
    const isExpanded = body.style.display !== 'none';
    const icon = document.getElementById(`toggle-icon-${round}`);
    
    if (isExpanded) {
        body.style.display = 'none';
        if (icon) icon.style.transform = 'rotate(0deg)';
        if (window._expandedRounds) window._expandedRounds.delete(round);
    } else {
        body.style.display = 'block';
        if (icon) icon.style.transform = 'rotate(180deg)';
        if (window._expandedRounds) window._expandedRounds.add(round);
    }
};

function disableTriggerButtons(disabled) {
    const ids = [
        'btn-run-daily-auto', 'btn-run-analysis-only', 'btn-run-execute',
        'btn-pb-run-execute', 'btn-pb-run-analysis',
        'btn-ha-run-execute', 'btn-ha-run-analysis'
    ];
    ids.forEach(id => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = disabled;
    });
}

function toKorDecision(dec) {
    if (dec === 'execute' || dec === 'approved') return '즉시 실행';
    if (dec === 'queue') return '승인 대기';
    if (dec === 'skip') return '수행 보류';
    return dec || '보류';
}

function toKorPlanCategory(category) {
    const labels = {
        position: '보유종목',
        candidate: '매수후보',
        ai_rebalance: 'AI 리밸런싱',
    };
    return labels[category] || category || 'AI 리밸런싱';
}

function formatKstTime(isoStr) {
    if (!isoStr) return '-';
    try {
        const d = new Date(isoStr);
        return d.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul' });
    } catch (e) {
        return isoStr;
    }
}

async function triggerSchedule(mode) {
    const btnId = mode === 'daily_auto' ? 'btn-run-daily-auto' : (mode === 'analysis_only' ? 'btn-run-analysis-only' : 'btn-run-execute');
    const btn = document.getElementById(btnId);
    if (!btn) return;
    
    setButtonBusy(btn, true);
    disableTriggerButtons(true);
    
    // Show running panel
    const runningPanel = document.getElementById('scheduler-running-panel');
    if (runningPanel) runningPanel.style.display = 'block';
    
    const logBox = document.getElementById('scheduler-running-log');
    if (logBox) {
        logBox.textContent = `[${new Date().toLocaleTimeString()}] ${mode} 스케쥴러 구동을 시작합니다. KIS API 호출 및 포트폴리오 분석으로 약 15~40초가 소요됩니다...\n`;
    }
    
    try {
        const strategyIds = getScheduledStrategyIds();
        const strategyId = getActiveStrategyId();
        const res = await postJson('/api/scheduler/run', {
            mode: mode,
            strategy_id: strategyIds.length ? null : (strategyId || null),
            strategy_ids: strategyIds,
            allowed_categories: ['position', 'candidate', 'ai_rebalance'],
        });
        if (res.status === 'started') {
            if (logBox) {
                logBox.textContent += `[${new Date().toLocaleTimeString()}] 스케쥴러 백그라운드 태스크가 성공적으로 등록되었습니다. 실시간 기동 중입니다.\n`;
            }
            startSchedulerPolling(mode);
        } else {
            throw new Error(res.detail || '기동 요청 거절됨');
        }
    } catch (err) {
        if (logBox) {
            logBox.textContent += `[에러] 기동 실패: ${err.message}\n`;
        }
        setStatus(`스케쥴 즉시실행 실패: ${err.message}`);
        disableTriggerButtons(false);
    } finally {
        setButtonBusy(btn, false);
    }
}

function startSchedulerPolling(mode) {
    if (schedulerPollInterval) return; // Already polling
    
    disableTriggerButtons(true);
    const runningPanel = document.getElementById('scheduler-running-panel');
    if (runningPanel) runningPanel.style.display = 'block';
    
    const logBox = document.getElementById('scheduler-running-log');
    
    let attempts = 0;
    schedulerPollInterval = setInterval(async () => {
        attempts++;
        try {
            const strategyId = getActiveStrategyId();
            const query = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : '';
            const data = await fetchJson(`/api/scheduler/status${query}`);
            const runState = data.run_state;
            
            if (!runState.is_running) {
                clearInterval(schedulerPollInterval);
                schedulerPollInterval = null;
                
                if (logBox) {
                    logBox.textContent += `[${new Date().toLocaleTimeString()}] 스케쥴러 실행이 완료되었습니다!\n`;
                    if (runState.error) {
                        logBox.textContent += `[오류] ${runState.error}\n`;
                        setStatus(`스케쥴러 실행 오류: ${runState.error}`);
                    } else {
                        logBox.textContent += `[성공] 실행이 정상 완료되었습니다.\n`;
                        setStatus('스케쥴러 구동이 성공적으로 완료되었습니다.', true);
                    }
                }
                
                // Force refresh all UI elements across different sections
                await renderScheduleInfo();
                if (typeof refreshOverview === 'function') refreshOverview();
                if (typeof renderSignals === 'function') renderSignals();
                if (typeof renderApprovals === 'function') renderApprovals();
                if (typeof loadScheduleHistoryData === 'function') loadScheduleHistoryData();
                if (typeof loadScanHistoryData === 'function') loadScanHistoryData();
                if (typeof loadPerformanceData === 'function') loadPerformanceData();
                if (typeof loadHeikinAshiScheduleHistory === 'function') loadHeikinAshiScheduleHistory();
                if (typeof loadHeikinAshiScanHistory === 'function') loadHeikinAshiScanHistory();
                if (typeof loadHeikinAshiPerformance === 'function') loadHeikinAshiPerformance();
                if (typeof renderWatchlist === 'function') renderWatchlist();
            } else {
                if (logBox) {
                    if (logBox.textContent.indexOf("스케쥴러 실행 중...") === -1 || attempts % 3 === 0) {
                        logBox.textContent = `[${new Date().toLocaleTimeString()}] ${runState.mode || mode} 모드로 스케쥴러 실행 중...\n(시작 시간: ${formatKstTime(runState.started_at)})\n`;
                        logBox.textContent += `[${new Date().toLocaleTimeString()}] 실행 중... (통신 및 분석 진행 중)\n`;
                    }
                }
            }
        } catch (e) {
            console.error("Failed to fetch scheduler status", e);
        }
    }, 3000);
}

function copySchedulerLog() {
    const logBox = document.getElementById('scheduler-running-log');
    if (!logBox) return;
    const text = logBox.innerText || logBox.textContent;
    
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById('btn-copy-scheduler-log');
        if (btn) {
            const originalText = btn.textContent;
            btn.textContent = '복사 완료!';
            btn.style.borderColor = '#10b981';
            btn.style.color = '#10b981';
            setTimeout(() => {
                btn.textContent = originalText;
                btn.style.borderColor = '';
                btn.style.color = '';
            }, 2000);
        }
    }).catch(err => {
        alert('로그 복사 실패: ' + err);
    });
}
