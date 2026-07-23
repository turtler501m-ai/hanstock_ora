const escapeHtml = (value) => {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
};

const setStatus = (message, ok = false) => {
    const banner = document.getElementById('status-banner');
    banner.hidden = false;
    banner.className = `status-banner ${ok ? 'ok' : ''}`;
    banner.textContent = message;
};

const setButtonBusy = (id, busy) => {
    const button = document.getElementById(id);
    if (button) {
        button.disabled = busy;
    }
};

let currentEnvFieldMap = {};

const NUMBER_UNIT_BY_KEY = {
    TOTAL_CAPITAL: 'KRW',
    MAX_POSITIONS: '개',
    MAX_SINGLE_WEIGHT: '비율',
    CASH_BUFFER: '비율',
    MAX_DAILY_LOSS_PCT: '%',
    SPLIT_N: '회',
    STOP_LOSS_PCT: '%',
    TAKE_PROFIT: '%',
    RSI_BUY: '점',
    RSI_SELL: '점',
    SCAN_UNIVERSE_SIZE: '개',
    KIS_CIRCUIT_COOLDOWN_SECONDS: '초',
    AI_SCORE_WEIGHT: '비율',
    AI_MIN_MODEL_CONFIDENCE: '비율',
    AI_CANDIDATE_LIMIT: '개',
    OPENAI_TIMEOUT_SECONDS: '초',
    USDKRW_FALLBACK_RATE: 'KRW/USD',
};

function isNumericField(field) {
    return field.type === 'int' || field.type === 'float';
}

function normalizeNumericText(value) {
    return String(value ?? '').replaceAll(',', '').trim();
}

function formatNumericText(value) {
    const normalized = normalizeNumericText(value);
    if (!normalized || normalized === '-' || !Number.isFinite(Number(normalized))) {
        return String(value ?? '');
    }
    const [integerPart, decimalPart] = normalized.split('.');
    const sign = integerPart.startsWith('-') ? '-' : '';
    const digits = sign ? integerPart.slice(1) : integerPart;
    const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return `${sign}${grouped}${decimalPart !== undefined ? `.${decimalPart}` : ''}`;
}

function unitForField(field) {
    if (field.key === 'MISTOCK_TOTAL_CAPITAL') {
        const currency = String(currentEnvFieldMap.MISTOCK_CURRENCY?.value || 'USD').toUpperCase();
        return currency || 'USD';
    }
    if (field.key === 'MISTOCK_CURRENCY') {
        return 'USD/KRW';
    }
    if (field.key.startsWith('MISTOCK_')) {
        const stripped = field.key.replace(/^MISTOCK_/, '');
        return NUMBER_UNIT_BY_KEY[stripped] || '';
    }
    return NUMBER_UNIT_BY_KEY[field.key] || '';
}

function wireNumericFormatting(root = document) {
    root.querySelectorAll('input[data-env-numeric="true"]').forEach((input) => {
        input.addEventListener('focus', () => {
            input.value = normalizeNumericText(input.value);
        });
        input.addEventListener('blur', () => {
            input.value = formatNumericText(input.value);
        });
    });
}

async function fetchJson(url) {
    const response = await fetch(url);
    const data = await response.json();
    if (!response.ok) {
        throw new Error(data.detail || `요청 실패: ${response.status}`);
    }
    return data;
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

function buildEnvControl(field) {
    const key = escapeHtml(field.key);
    const label = escapeHtml(field.label || field.key);
    const value = escapeHtml(isNumericField(field) ? formatNumericText(field.value || '') : (field.value || ''));
    const hint = escapeHtml(field.hint || '');
    const unit = unitForField(field);
    const unitMarkup = unit ? `<span class="env-input-unit">${escapeHtml(unit)}</span>` : '';
    const hintText = unit ? `${hint}${hint ? ' · ' : ''}단위: ${escapeHtml(unit)}` : hint;

    if (field.type === 'bool') {
        const selected = String(field.value || '').toLowerCase() === 'true';
        return `
            <div class="env-field">
                <label for="env-${key}">${label}</label>
                <select id="env-${key}" data-env-key="${key}" data-original="${escapeHtml(field.value || '')}">
                    <option value="true" ${selected ? 'selected' : ''}>true</option>
                    <option value="false" ${!selected ? 'selected' : ''}>false</option>
                </select>
                <small>${hint}</small>
            </div>
        `;
    }

    if (field.type === 'select') {
        const options = (field.options || []).map((option) => {
            const selected = String(field.value || '') === String(option);
            return `<option value="${escapeHtml(option)}" ${selected ? 'selected' : ''}>${escapeHtml(option)}</option>`;
        }).join('');
        return `
            <div class="env-field">
                <label for="env-${key}">${label}</label>
                <select id="env-${key}" data-env-key="${key}" data-original="${escapeHtml(field.value || '')}">
                    ${options}
                </select>
                <small>${hint}</small>
            </div>
        `;
    }

    const inputType = isNumericField(field) ? 'text' : 'text';
    const inputMode = isNumericField(field) ? ' inputmode="decimal"' : '';
    const numericAttrs = isNumericField(field) ? ' data-env-numeric="true"' : '';
    return `
        <div class="env-field">
            <label for="env-${key}">${label}</label>
            <div class="env-input-with-unit">
                <input id="env-${key}" type="${inputType}"${inputMode} value="${value}"
                    data-env-key="${key}" data-env-type="${escapeHtml(field.type)}"${numericAttrs}
                    data-original="${escapeHtml(field.value || '')}" autocomplete="off">
                ${unitMarkup}
            </div>
            <small>${hintText}</small>
        </div>
    `;
}

const CATEGORIES = [
    {
        title: "🇰🇷 국내주식 자동매매 설정 (한스톡)",
        short: "국내주식 (한스톡)",
        keys: [
            "ONLINE_ACCESS_BLOCKED",
            "TRADING_ENV",
            "DRY_RUN",
            "ENABLE_LIVE_TRADING",
            "REQUIRE_APPROVAL",
            "TOTAL_CAPITAL",
            "MAX_POSITIONS",
            "MAX_SINGLE_WEIGHT",
            "CASH_BUFFER",
            "MAX_DAILY_LOSS_PCT",
            "HANSTOCK_EXCLUDED_SYMBOLS",
            "KIS_ORDER_MIN_INTERVAL_SECONDS",
            "SPLIT_N",
            "STOP_LOSS_PCT",
            "TAKE_PROFIT",
            "RSI_BUY",
            "RSI_SELL",
            "SCAN_UNIVERSE_SIZE",
            "TRADE_DB_PATH",
            "ACTIVE_MODEL_VERSION"
        ]
    },
    {
        title: "🇺🇸 미국주식 자동매매 설정 (미스톡)",
        short: "미국주식 (미스톡)",
        keys: [
            "MISTOCK_TRADING_ENV",
            "MISTOCK_DRY_RUN",
            "MISTOCK_ENABLE_LIVE_TRADING",
            "MISTOCK_REQUIRE_APPROVAL",
            "MISTOCK_TOTAL_CAPITAL",
            "MISTOCK_MARKET",
            "MISTOCK_TRADE_DB_PATH",
            "MISTOCK_UNIVERSE"
        ]
    },
    {
        title: "🔑 한국투자증권(KIS) API 연동 정보",
        short: "증권사 API",
        keys: [
            "KISTOCK_APP_KEY",
            "KISTOCK_APP_SECRET",
            "KISTOCK_ACCOUNT",
            "KISTOCK_HTS_ID",
            "KIS_WEBSOCKET_ENABLED",
            "KIS_CONDITION_SEARCH_ENABLED",
            "KIS_CONDITION_USER_ID",
            "KIS_CONDITION_SEQ",
            "KIS_CONDITION_NAME",
            "KIS_CIRCUIT_COOLDOWN_SECONDS"
        ]
    },
    {
        title: "🤖 AI 모델 및 OpenAI 연동",
        short: "AI & OpenAI",
        keys: [
            "AI_STRATEGY_ENABLED",
            "AI_SCORE_WEIGHT",
            "AI_MIN_MODEL_CONFIDENCE",
            "AI_REQUIRE_BACKTEST_PASS",
            "AI_AUTO_APPROVE",
            "AI_CANDIDATE_LIMIT",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_TIMEOUT_SECONDS"
        ]
    },
    {
        title: "📢 알림 및 수집기 설정 (Slack / Telegram)",
        short: "알림 & 수집기",
        keys: [
            "SLACK_WEBHOOK_URL",
            "MISTOCK_SLACK_WEBHOOK_URL",
            "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH",
            "TELEGRAM_SESSION_NAME",
            "TELEGRAM_TARGET_CHANNELS"
        ]
    },
    {
        title: "💵 환율 및 통화 설정 (Exchange Rate)",
        short: "환율 & 통화",
        keys: [
            "MISTOCK_CURRENCY",
            "USDKRW_FALLBACK_RATE",
            "MISTOCK_EXCHANGE_MAP"
        ]
    }
];

let currentActiveTabIndex = 0;

async function renderEnvSettings() {
    try {
        const data = await fetchJson('/api/env');
        const fields = data.fields || [];
        const fieldMap = {};
        fields.forEach(f => {
            fieldMap[f.key] = f;
        });
        currentEnvFieldMap = fieldMap;

        let html = '';
        let tabsHtml = '';
        const categorisedKeys = new Set();

        CATEGORIES.forEach((cat, index) => {
            const catFields = cat.keys
                .map(k => fieldMap[k])
                .filter(Boolean);

            if (catFields.length > 0) {
                cat.keys.forEach(k => categorisedKeys.add(k));
                
                const isActive = index === currentActiveTabIndex;
                tabsHtml += `<button type="button" class="env-tab-button ${isActive ? 'active' : ''}" data-tab-index="${index}">${cat.short}</button>`;

                let rateInfoCard = '';
                if (index === 5 && data.rate_info) {
                    rateInfoCard = `
                        <div class="env-info-card" style="margin-bottom: 1.5rem; padding: 1.25rem; background: rgba(96, 165, 250, 0.08); border: 1px solid rgba(96, 165, 250, 0.15); border-radius: 8px; display: flex; gap: 2rem; align-items: center;">
                            <div style="font-size: 1.5rem;">💵</div>
                            <div>
                                <h4 style="margin: 0 0 0.25rem 0; font-size: 1rem; color: #f3f4f6; font-weight: 600;">실시간 적용 환율 정보</h4>
                                <div style="display: flex; gap: 1.5rem; font-size: 0.88rem; color: #9ca3af; flex-wrap: wrap;">
                                    <span>현재 적용 환율: <strong style="color: #60a5fa; font-size: 1rem;">₩${data.rate_info.current_rate}</strong></span>
                                    <span>마지막 동기화 시각: <strong style="color: #f3f4f6;">${data.rate_info.last_fetch_time}</strong></span>
                                </div>
                            </div>
                        </div>
                    `;
                }

                html += `
                    <div class="env-category-section" id="env-sec-${index}" style="display: ${isActive ? 'block' : 'none'};">
                        <h3 class="env-category-title" style="margin-bottom: 1.25rem; color: #60a5fa; font-size: 1.15rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem; text-shadow: 0 0 10px rgba(96,165,250,0.1);">
                            ${cat.title}
                        </h3>
                        ${rateInfoCard}
                        <div class="env-grid env-page-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.25rem;">
                            ${catFields.map(buildEnvControl).join('')}
                        </div>
                    </div>
                `;
            }
        });

        // Render uncategorized fields if any
        const uncategorisedFields = fields.filter(f => !categorisedKeys.has(f.key));
        const miscIndex = CATEGORIES.length;
        if (uncategorisedFields.length > 0) {
            const isActive = miscIndex === currentActiveTabIndex;
            tabsHtml += `<button type="button" class="env-tab-button ${isActive ? 'active' : ''}" data-tab-index="${miscIndex}">기타 설정</button>`;
            
            html += `
                <div class="env-category-section" id="env-sec-${miscIndex}" style="display: ${isActive ? 'block' : 'none'};">
                    <h3 class="env-category-title" style="margin-bottom: 1.25rem; color: #9ca3af; font-size: 1.15rem; font-weight: 600; display: flex; align-items: center; gap: 0.5rem;">
                        ⚙️ 기타 설정
                    </h3>
                    <div class="env-grid env-page-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.25rem;">
                        ${uncategorisedFields.map(buildEnvControl).join('')}
                    </div>
                </div>
            `;
        }

        const tabsEl = document.getElementById('env-tabs');
        if (tabsEl) {
            tabsEl.innerHTML = tabsHtml;
            
            tabsEl.querySelectorAll('.env-tab-button').forEach(btn => {
                btn.addEventListener('click', () => {
                    const tabIdx = parseInt(btn.dataset.tabIndex, 10);
                    currentActiveTabIndex = tabIdx;
                    
                    tabsEl.querySelectorAll('.env-tab-button').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    
                    document.querySelectorAll('.env-category-section').forEach(sec => {
                        sec.style.display = 'none';
                    });
                    const targetSec = document.getElementById(`env-sec-${tabIdx}`);
                    if (targetSec) {
                        targetSec.style.display = 'block';
                    }
                });
            });
        }

        const envGridEl = document.getElementById('env-grid');
        if (envGridEl) {
            envGridEl.style.display = 'block';
            envGridEl.innerHTML = html;
            wireNumericFormatting(envGridEl);
        }
        document.getElementById('env-meta').textContent = `${data.path || '.env'} · 저장 즉시 런타임 반영, 일부 값은 서버 재시작 필요`;
        const onlineBlocked = String(fieldMap.ONLINE_ACCESS_BLOCKED?.value || '').toLowerCase() === 'true';
        updateOnlineBlockButton(onlineBlocked);
        if (onlineBlocked) {
            setStatus('온라인 접속 차단 중: 주문과 외부 API 갱신은 중지되며 DB 저장 정보만 표시됩니다.');
        }
    } catch (err) {
        setStatus(`환경설정 불러오기 실패: ${err.message}`);
    }
}

async function saveEnvSettings(event) {
    event.preventDefault();
    const values = {};
    document.querySelectorAll('[data-env-key]').forEach((input) => {
        const key = input.dataset.envKey;
        const original = input.dataset.original || '';
        const value = input.dataset.envNumeric === 'true'
            ? normalizeNumericText(input.value)
            : input.value;
        const comparableOriginal = input.dataset.envNumeric === 'true'
            ? normalizeNumericText(original)
            : original;
        if (value !== comparableOriginal) {
            values[key] = value;
        }
    });

    if (!Object.keys(values).length) {
        setStatus('변경된 환경설정이 없습니다.', true);
        return;
    }

    const updateList = Object.keys(values).sort().join(', ');
    const confirmed = window.confirm(`환경설정을 저장하시겠습니까?\n\n변경 항목: ${updateList}`);
    if (!confirmed) {
        setStatus('환경설정 저장을 취소했습니다.');
        return;
    }

    setButtonBusy('btn-env-save', true);
    try {
        const result = await postJson('/api/env', { values });
        await renderEnvSettings();
        const restartText = result.requires_restart ? '서버 재시작 후 완전히 적용됩니다.' : '현재 실행 중인 대시보드에도 반영되었습니다.';
        setStatus(`환경설정을 저장했습니다: ${result.updated.join(', ')}. ${restartText}`, true);
    } catch (err) {
        setStatus(`환경설정 저장 실패: ${err.message}`);
    } finally {
        setButtonBusy('btn-env-save', false);
    }
}

async function resetDatabaseAndClearCache() {
    const confirmed = window.confirm(
        "⚠️ [경고] 모든 국내/미국 주식 거래 내역 DB와 토큰/잔고 캐시 파일을 완전히 초기화하시겠습니까?\n\n- 기존 DB 파일은 백업본으로 안전하게 이동 처리됩니다.\n- 초기화 후에는 이전 성과 데이터 조회가 불가능합니다."
    );
    if (!confirmed) {
        return;
    }
    
    setButtonBusy('btn-env-reset-db', true);
    try {
        const result = await postJson('/api/config/reset-database');
        if (result.ok) {
            alert(`${result.message}\n\n상세 조치 내역:\n${result.details.map(d => '• ' + d).join('\n')}`);
            window.location.reload();
        } else {
            throw new Error(result.message || '초기화 실패');
        }
    } catch (err) {
        alert(`초기화 작업 중 오류가 발생했습니다:\n${err.message}`);
    } finally {
        setButtonBusy('btn-env-reset-db', false);
    }
}

function updateOnlineBlockButton(blocked) {
    const button = document.getElementById('btn-env-online-block');
    if (!button) {
        return;
    }
    button.textContent = blocked ? '온라인 차단 해제' : '온라인 차단';
    button.dataset.blocked = blocked ? 'true' : 'false';
}

async function toggleOnlineAccessBlocked() {
    const button = document.getElementById('btn-env-online-block');
    const currentlyBlocked = String(button?.dataset.blocked || currentEnvFieldMap.ONLINE_ACCESS_BLOCKED?.value || '').toLowerCase() === 'true';
    const nextValue = currentlyBlocked ? 'false' : 'true';
    const confirmed = window.confirm(
        currentlyBlocked
            ? '온라인 접속 차단을 해제하시겠습니까?'
            : '온라인 접속을 차단하시겠습니까?\n\n주문, 승인 실행, 외부 API, 웹소켓, 알림 연동이 차단됩니다.'
    );
    if (!confirmed) {
        return;
    }

    setButtonBusy('btn-env-online-block', true);
    try {
        await postJson('/api/env', { values: { ONLINE_ACCESS_BLOCKED: nextValue } });
        await renderEnvSettings();
        setStatus(nextValue === 'true' ? '온라인 접속 차단을 켰습니다.' : '온라인 접속 차단을 해제했습니다.', true);
    } catch (err) {
        setStatus(`온라인 접속 차단 설정 실패: ${err.message}`);
    } finally {
        setButtonBusy('btn-env-online-block', false);
    }
}

document.getElementById('env-form').addEventListener('submit', saveEnvSettings);
document.getElementById('btn-env-reload').addEventListener('click', renderEnvSettings);
const resetBtn = document.getElementById('btn-env-reset-db');
if (resetBtn) {
    resetBtn.addEventListener('click', resetDatabaseAndClearCache);
}
const onlineBlockBtn = document.getElementById('btn-env-online-block');
if (onlineBlockBtn) {
    onlineBlockBtn.addEventListener('click', toggleOnlineAccessBlocked);
}
renderEnvSettings();
