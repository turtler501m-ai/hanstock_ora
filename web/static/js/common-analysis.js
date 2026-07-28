const commonTabRefreshState = new Map();
const ISOLATED_STRATEGY_IDS = new Set([
    'plunge_bounce_strategy',
    'heikin_ashi_scalping_strategy',
]);
let activeAnalysisCycle = null;
const analysisCyclePromises = new Map();
let strategyRequestGeneration = 0;
const ANALYSIS_CYCLE_MAX_AGE_MS = 15 * 60 * 1000;

function getActiveStrategyId() {
    return document.getElementById('select-ai-ranker')?.value
        || localStorage.getItem('hanstock_ai_ranker')
        || '';
}

function withQuery(path, params = {}) {
    const url = new URL(path, window.location.origin);
    Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
            url.searchParams.set(key, String(value));
        }
    });
    return `${url.pathname}${url.search}`;
}

function withActiveStrategy(path, params = {}) {
    const url = new URL(withQuery(path, params), window.location.origin);
    const strategyId = getActiveStrategyId();
    if (strategyId && !ISOLATED_STRATEGY_IDS.has(strategyId)) {
        url.searchParams.set('strategy_id', strategyId);
    }
    return `${url.pathname}${url.search}`;
}

function performancePath(path, params = {}) {
    const scope = document.getElementById('select-performance-scope')?.value || 'account';
    return scope === 'strategy' ? withActiveStrategy(path, params) : withQuery(path, params);
}

async function ensureCommonAnalysisCycle({ force = false } = {}) {
    const strategyId = getActiveStrategyId();
    const generation = strategyRequestGeneration;
    if (!strategyId || ISOLATED_STRATEGY_IDS.has(strategyId)) return null;
    const activeCycleTime = Date.parse(activeAnalysisCycle?.updated_at || activeAnalysisCycle?.created_at || '');
    const activeCycleFresh = Number.isFinite(activeCycleTime)
        && Date.now() - activeCycleTime <= ANALYSIS_CYCLE_MAX_AGE_MS;
    if (!force && activeAnalysisCycle?.strategy_id === strategyId && activeCycleFresh) {
        return activeAnalysisCycle;
    }
    if (!force && analysisCyclePromises.has(strategyId)) return analysisCyclePromises.get(strategyId);

    const promise = (async () => {
        if (!force) {
            const context = await fetchJson(withActiveStrategy('/api/strategy-context'));
            const existing = context.analysis_flow?.cycle;
            const cycleTime = Date.parse(existing?.updated_at || existing?.created_at || '');
            const fresh = Number.isFinite(cycleTime) && Date.now() - cycleTime <= ANALYSIS_CYCLE_MAX_AGE_MS;
            if (existing?.strategy_id === strategyId && existing.status !== 'failed' && fresh) {
                if (generation !== strategyRequestGeneration || getActiveStrategyId() !== strategyId) return null;
                activeAnalysisCycle = existing;
                return existing;
            }
        }
        const result = await postJson('/api/analysis-cycles', {
            strategy_id: strategyId,
            mode: 'analysis',
        });
        if (generation !== strategyRequestGeneration || getActiveStrategyId() !== strategyId) return null;
        activeAnalysisCycle = result.cycle || null;
        return activeAnalysisCycle;
    })().finally(() => {
        if (analysisCyclePromises.get(strategyId) === promise) {
            analysisCyclePromises.delete(strategyId);
        }
    });
    analysisCyclePromises.set(strategyId, promise);
    return promise;
}

async function commonAnalysisPath(path, params = {}) {
    const cycle = await ensureCommonAnalysisCycle();
    return withActiveStrategy(path, { ...params, cycle_id: cycle?.id || '' });
}

async function startCommonAnalysisRefresh() {
    await ensureCommonAnalysisCycle({ force: true });
    ['signals', 'execution-plan', 'candidate-history'].forEach((key) => commonTabRefreshState.delete(key));
    await renderCandidates({ refresh: true });
    return Promise.all([renderSignals(), renderExecutionPlan()]);
}

async function refreshCommonAnalysisViews() {
    const cycle = await ensureCommonAnalysisCycle();
    if (!cycle?.stages?.candidates) await renderCandidates();
    return Promise.all([
        renderSignals(),
        renderExecutionPlan(),
        renderCandidateHistory(),
    ]);
}

function invalidateCommonTabRefreshes() {
    commonTabRefreshState.clear();
    activeAnalysisCycle = null;
    strategyRequestGeneration += 1;
}

function captureAnalysisCycle(data) {
    if (
        data?._analysis_cycle?.id
        && data._analysis_cycle.strategy_id === getActiveStrategyId()
    ) {
        activeAnalysisCycle = data._analysis_cycle;
    }
}

function captureStrategyRequest() {
    return {
        strategyId: getActiveStrategyId(),
        generation: strategyRequestGeneration,
    };
}

function isCurrentStrategyRequest(request) {
    return request.strategyId === getActiveStrategyId()
        && request.generation === strategyRequestGeneration;
}

function runCommonTabRefresh(key, refresh, { force = false, maxAgeMs = 15000 } = {}) {
    const strategyId = getActiveStrategyId();
    const state = commonTabRefreshState.get(key);
    if (state?.promise && state.strategyId === strategyId) return state.promise;
    if (!force && state?.strategyId === strategyId && Date.now() - state.finishedAt < maxAgeMs) {
        return Promise.resolve();
    }

    const nextState = { strategyId, finishedAt: 0, promise: null };
    nextState.promise = Promise.resolve()
        .then(refresh)
        .then((result) => {
            nextState.finishedAt = Date.now();
            return result;
        })
        .finally(() => {
            nextState.promise = null;
        });
    commonTabRefreshState.set(key, nextState);
    return nextState.promise;
}

function getActiveDashboardTab() {
    return document.querySelector('.dashboard-tab.active')?.dataset.dashboardTab || 'overview';
}

async function refreshCommonDashboardTab(target, options = {}) {
    const force = Boolean(options.force);
    const refresh = (key, task, maxAgeMs = 15000) => (
        runCommonTabRefresh(key, task, { force, maxAgeMs })
    );

    if (target === 'overview' || target === 'portfolio') return refresh('balance', renderBalance);
    if (target === 'watchlist') return refresh('watchlist', renderWatchlist);
    if (target === 'ai-strategies') {
        return Promise.all([
            refresh('strategy-context', renderStrategyContext),
            refresh('ai-strategies', renderAiStrategies),
        ]);
    }
    if (target === 'strategy') {
        return refresh('strategy-analysis', refreshCommonAnalysisViews, 30000);
    }
    if (target === 'schedule') return refresh('schedule', renderScheduleInfo);
    if (target === 'orders') return refresh('approvals', renderApprovals);
    if (target === 'performance') return refresh('performance', renderTrades, 30000);
    return Promise.resolve();
}
