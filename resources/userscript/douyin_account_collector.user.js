// ==UserScript==
// @name         DouK 抖音账号采集器 v2.4.9
// @namespace    douk-account-collector
// @version      2.4.9
// @description  JSON＋Excel 严格同步采集、分类统计与添加成功后的编号截图。
// @match        https://www.douyin.com/user/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
    'use strict';

    // Hard guards: never run inside iframes and never inject twice in one page.
    if (window.top !== window.self) return;
    if (window.__DOUK_ACCOUNT_COLLECTOR_V249_RUNNING__) return;
    window.__DOUK_ACCOUNT_COLLECTOR_V249_RUNNING__ = true;

    const VERSION = '2.4.9';
    const API_BASE = 'http://127.0.0.1:8765';
    const TOKEN = 'DOUK_COLLECTOR_V248_20260719';
    const PANEL_ID = 'douk-account-collector-v249';
    const TAB_ID = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const PANEL_STORAGE_KEY = 'douk-account-collector-panel-v245';
    const PANEL_MARGIN = 14;
    const MIN_PANEL_WIDTH = 320;
    const MIN_PANEL_HEIGHT = 260;
    const PANEL_FONT_LEVELS = ['standard', 'large', 'xlarge'];
    const PANEL_FONT_LABELS = {
        standard: '13px（默认）',
        large: '15px',
        xlarge: '17px',
    };
    const CATEGORY_NAMES = ['顶级', '次顶级', '普通'];
    const PREVIEW_CONNECTION_ATTEMPTS = 3;
    const PREVIEW_RETRY_DELAYS = [450, 900];

    let currentProfileKey = '';
    let lastCompletedProfileKey = '';
    let activePreviewKey = '';
    let previewTimer = null;
    let recognizeTimer = null;
    let previewRequestInFlight = false;
    let queuedPreview = false;
    let queuedForcePreview = false;
    let previewRequestSerial = 0;
    let isAdding = false;
    let isClassifying = false;
    let currentCategoryState = null;
    let markEditedByUser = false;
    let screenshotReady = false;
    let defaultPanelState = null;
    let panelState = loadPanelState();
    let panelWindowEventsBound = false;

    function pageIsVisible() {
        return document.visibilityState === 'visible';
    }

    function cleanText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function readStorage() {
        try {
            return localStorage.getItem(PANEL_STORAGE_KEY);
        } catch (_) {
            return null;
        }
    }

    function writeStorage(value) {
        try {
            localStorage.setItem(PANEL_STORAGE_KEY, value);
        } catch (_) {
            // Ignore storage failures in restricted/private contexts.
        }
    }

    function clampNumber(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function normalizePanelFontSize(value) {
        // 旧版 small（12px）已经取消，读取到时自动迁移到默认 13px。
        if (value === 'small') return 'standard';
        return PANEL_FONT_LEVELS.includes(value) ? value : 'standard';
    }

    function loadPanelState() {
        const raw = readStorage();
        if (!raw) {
            return {
                theme: 'dark',
                left: null,
                top: null,
                width: null,
                height: null,
                minimized: false,
                docked: true,
                fontSize: 'standard',
            };
        }
        try {
            const parsed = JSON.parse(raw);
            return {
                theme: parsed.theme === 'light' ? 'light' : 'dark',
                left: Number.isFinite(parsed.left) ? parsed.left : null,
                top: Number.isFinite(parsed.top) ? parsed.top : null,
                width: Number.isFinite(parsed.width) ? parsed.width : null,
                height: Number.isFinite(parsed.height) ? parsed.height : null,
                minimized: parsed.minimized === true,
                docked: typeof parsed.docked === 'boolean' ? parsed.docked : null,
                fontSize: normalizePanelFontSize(parsed.fontSize),
            };
        } catch (_) {
            return {
                theme: 'dark',
                left: null,
                top: null,
                width: null,
                height: null,
                minimized: false,
                docked: true,
                fontSize: 'standard',
            };
        }
    }

    function savePanelState() {
        writeStorage(JSON.stringify(panelState));
    }

    function getDefaultPanelState(panel) {
        const rect = panel.getBoundingClientRect();
        return {
            theme: 'dark',
            left: Math.max(PANEL_MARGIN, Math.round(window.innerWidth - rect.width - PANEL_MARGIN)),
            top: Math.max(PANEL_MARGIN, Math.round(window.innerHeight - rect.height - PANEL_MARGIN)),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            minimized: false,
            docked: true,
            fontSize: 'standard',
        };
    }

    function clampPanelPosition(left, top, width, height) {
        const safeLeft = clampNumber(
            Math.round(left),
            PANEL_MARGIN,
            Math.max(PANEL_MARGIN, window.innerWidth - width - PANEL_MARGIN)
        );
        const safeTop = clampNumber(
            Math.round(top),
            PANEL_MARGIN,
            Math.max(PANEL_MARGIN, window.innerHeight - height - PANEL_MARGIN)
        );
        return { left: safeLeft, top: safeTop };
    }

    function clampPanelRect(left, top, width, height) {
        const safeWidth = clampNumber(
            Math.round(width),
            MIN_PANEL_WIDTH,
            Math.max(MIN_PANEL_WIDTH, window.innerWidth - PANEL_MARGIN * 2)
        );
        const safeHeight = clampNumber(
            Math.round(height),
            MIN_PANEL_HEIGHT,
            Math.max(MIN_PANEL_HEIGHT, window.innerHeight - PANEL_MARGIN * 2)
        );
        const position = clampPanelPosition(left, top, safeWidth, safeHeight);
        return { ...position, width: safeWidth, height: safeHeight };
    }

    function inferDockedState(state) {
        if (!defaultPanelState) return true;
        const width = state.width ?? defaultPanelState.width;
        const height = state.height ?? defaultPanelState.height;
        const left = state.left ?? defaultPanelState.left;
        const top = state.top ?? defaultPanelState.top;
        const expected = clampPanelPosition(
            window.innerWidth - width - PANEL_MARGIN,
            window.innerHeight - height - PANEL_MARGIN,
            width,
            height
        );
        const tolerance = 32;
        return Math.abs(left - expected.left) <= tolerance
            && Math.abs(top - expected.top) <= tolerance;
    }

    function updateThemeButton(panel) {
        const themeButton = panel.querySelector('#douk-theme');
        if (!themeButton) return;
        const isLight = panel.dataset.theme === 'light';
        themeButton.textContent = '主题';
        themeButton.title = isLight ? '切换到黑色背景' : '切换到白色背景';
    }

    function updateFontSizeButton(panel) {
        const fontSizeButton = panel.querySelector('#douk-font-size');
        if (!fontSizeButton) return;
        const fontSize = normalizePanelFontSize(panel.dataset.fontSize);
        fontSizeButton.textContent = 'A';
        fontSizeButton.title = `切换面板字号，当前：${PANEL_FONT_LABELS[fontSize]}`;
    }

    function updateMinimizeButton(panel) {
        const minimizeButton = panel.querySelector('#douk-minimize');
        const body = panel.querySelector('#douk-body');
        const resizeHandles = panel.querySelectorAll('.douk-resize');
        const minimized = panel.dataset.minimized === 'true';
        if (body) body.style.display = minimized ? 'none' : '';
        resizeHandles.forEach((handle) => {
            handle.style.display = minimized ? 'none' : '';
        });
        if (minimizeButton) {
            minimizeButton.textContent = minimized ? '+' : '-';
            minimizeButton.title = minimized ? '展开面板' : '收起面板';
        }
    }

    function persistPanelRect(panel, includeHeight = true) {
        const rect = panel.getBoundingClientRect();
        const minimized = panel.dataset.minimized === 'true';
        const docked = panelState.docked !== false;
        // 停靠状态收起时，屏幕上的坐标属于标题栏；不要覆盖展开状态的坐标。
        if (!(docked && minimized)) {
            panelState.left = Math.round(rect.left);
            panelState.top = Math.round(rect.top);
        }
        panelState.width = Math.round(rect.width);
        if (includeHeight && !minimized) {
            panelState.height = Math.round(rect.height);
        }
        panelState.theme = panel.dataset.theme === 'light' ? 'light' : 'dark';
        panelState.minimized = minimized;
        panelState.docked = docked;
        panelState.fontSize = normalizePanelFontSize(panel.dataset.fontSize);
        savePanelState();
    }

    function applyPanelState(panel) {
        if (!defaultPanelState) {
            defaultPanelState = getDefaultPanelState(panel);
        }
        const desiredTheme = panelState.theme === 'light' ? 'light' : defaultPanelState.theme;
        const desiredFontSize = normalizePanelFontSize(panelState.fontSize);
        const desiredWidth = panelState.width || defaultPanelState.width;
        const desiredHeight = panelState.height || defaultPanelState.height;
        const desiredLeft = panelState.left ?? defaultPanelState.left;
        const desiredTop = panelState.top ?? defaultPanelState.top;
        const rect = clampPanelRect(desiredLeft, desiredTop, desiredWidth, desiredHeight);
        if (typeof panelState.docked !== 'boolean') {
            // 兼容旧版本保存的状态：原来位于右下角的视为停靠，否则视为自由位置。
            panelState.docked = inferDockedState(panelState);
        }

        panel.style.right = 'auto';
        panel.style.bottom = 'auto';
        panel.style.width = `${rect.width}px`;
        panel.style.height = panelState.minimized ? 'auto' : `${rect.height}px`;
        panel.dataset.theme = desiredTheme;
        panel.dataset.fontSize = desiredFontSize;
        panel.dataset.minimized = panelState.minimized ? 'true' : 'false';
        updateMinimizeButton(panel);

        const effectiveHeight = panelState.minimized
            ? Math.max(1, Math.round(panel.getBoundingClientRect().height))
            : rect.height;
        const position = panelState.docked
            ? clampPanelPosition(
                window.innerWidth - rect.width - PANEL_MARGIN,
                window.innerHeight - effectiveHeight - PANEL_MARGIN,
                rect.width,
                effectiveHeight
            )
            : clampPanelPosition(rect.left, rect.top, rect.width, effectiveHeight);
        panel.style.left = `${position.left}px`;
        panel.style.top = `${position.top}px`;
        updateThemeButton(panel);
        updateFontSizeButton(panel);
    }

    function resetPanelState(panel) {
        if (!defaultPanelState) {
            defaultPanelState = getDefaultPanelState(panel);
        }
        panelState.left = defaultPanelState.left;
        panelState.top = defaultPanelState.top;
        panelState.width = defaultPanelState.width;
        panelState.height = defaultPanelState.height;
        panelState.minimized = false;
        panelState.docked = true;
        applyPanelState(panel);
        persistPanelRect(panel);
    }

    function togglePanelTheme(panel) {
        panelState.theme = panel.dataset.theme === 'light' ? 'dark' : 'light';
        panel.dataset.theme = panelState.theme;
        updateThemeButton(panel);
        persistPanelRect(panel, false);
    }

    function cyclePanelFontSize(panel) {
        const current = normalizePanelFontSize(panel.dataset.fontSize);
        const currentIndex = PANEL_FONT_LEVELS.indexOf(current);
        panelState.fontSize = PANEL_FONT_LEVELS[(currentIndex + 1) % PANEL_FONT_LEVELS.length];
        applyPanelState(panel);
        persistPanelRect(panel, false);
    }

    function setPanelMinimized(panel, minimized) {
        const wasMinimized = panel.dataset.minimized === 'true';
        if (wasMinimized === minimized) return;
        if (minimized) {
            // 收起前保存完整面板的尺寸和自由位置，展开时原样恢复。
            persistPanelRect(panel);
        }
        panelState.minimized = minimized;
        applyPanelState(panel);
        persistPanelRect(panel, false);
    }

    function startDrag(panel, event) {
        if (event.button !== 0) return;
        if (event.target.closest('button')) return;
        const rect = panel.getBoundingClientRect();
        const startX = event.clientX;
        const startY = event.clientY;
        const startLeft = rect.left;
        const startTop = rect.top;
        let moved = false;

        const onMove = (moveEvent) => {
            const dx = moveEvent.clientX - startX;
            const dy = moveEvent.clientY - startY;
            if (!moved && Math.abs(dx) < 2 && Math.abs(dy) < 2) return;
            moved = true;
            panelState.docked = false;
            const nextLeft = startLeft + dx;
            const nextTop = startTop + dy;
            const currentRect = panel.getBoundingClientRect();
            const clamped = clampPanelPosition(
                nextLeft,
                nextTop,
                currentRect.width,
                currentRect.height
            );
            panel.style.left = `${clamped.left}px`;
            panel.style.top = `${clamped.top}px`;
        };

        let finished = false;
        const finish = () => {
            if (finished) return;
            finished = true;
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', finish);
            window.removeEventListener('blur', finish);
            document.removeEventListener('mouseleave', finish);
            if (moved) {
                persistPanelRect(panel, panel.dataset.minimized !== 'true');
            }
        };

        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', finish);
        window.addEventListener('blur', finish);
        document.addEventListener('mouseleave', finish);
        event.preventDefault();
    }

    function startResize(panel, handle, event) {
        if (event.button !== 0) return;
        if (panel.dataset.minimized === 'true') return;
        const direction = handle.dataset.resize || '';
        const rect = panel.getBoundingClientRect();
        const startX = event.clientX;
        const startY = event.clientY;
        const startLeft = rect.left;
        const startTop = rect.top;
        const startWidth = rect.width;
        const startHeight = rect.height;

        const onMove = (moveEvent) => {
            const dx = moveEvent.clientX - startX;
            const dy = moveEvent.clientY - startY;
            let left = startLeft;
            let top = startTop;
            let width = startWidth;
            let height = startHeight;

            if (direction.includes('e')) {
                width = startWidth + dx;
            }
            if (direction.includes('s')) {
                height = startHeight + dy;
            }
            if (direction.includes('w')) {
                width = startWidth - dx;
                left = startLeft + dx;
            }
            if (direction.includes('n')) {
                height = startHeight - dy;
                top = startTop + dy;
            }

            width = clampNumber(width, MIN_PANEL_WIDTH, Math.max(MIN_PANEL_WIDTH, window.innerWidth - PANEL_MARGIN * 2));
            height = clampNumber(height, MIN_PANEL_HEIGHT, Math.max(MIN_PANEL_HEIGHT, window.innerHeight - PANEL_MARGIN * 2));

            if (direction.includes('w')) {
                left = startLeft + (startWidth - width);
            }
            if (direction.includes('n')) {
                top = startTop + (startHeight - height);
            }

            const clamped = clampPanelRect(left, top, width, height);
            panel.style.left = `${clamped.left}px`;
            panel.style.top = `${clamped.top}px`;
            panel.style.width = `${clamped.width}px`;
            panel.style.height = `${clamped.height}px`;
        };

        let finished = false;
        const finish = () => {
            if (finished) return;
            finished = true;
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', finish);
            window.removeEventListener('blur', finish);
            document.removeEventListener('mouseleave', finish);
            if (panelState.docked) {
                applyPanelState(panel);
            }
            persistPanelRect(panel);
        };

        window.addEventListener('mousemove', onMove);
        window.addEventListener('mouseup', finish);
        window.addEventListener('blur', finish);
        document.addEventListener('mouseleave', finish);
        event.preventDefault();
        event.stopPropagation();
    }

    function bindPanelWindowEvents() {
        if (panelWindowEventsBound) return;
        panelWindowEventsBound = true;
        window.addEventListener('resize', () => {
            const panel = document.getElementById(PANEL_ID);
            if (!panel) return;
            // Window-size changes may temporarily clamp the panel, but they
            // are not user layout edits and must never overwrite shared state.
            applyPanelState(panel);
        });
        window.addEventListener('storage', (event) => {
            if (event.key !== PANEL_STORAGE_KEY) return;
            panelState = loadPanelState();
            const panel = document.getElementById(PANEL_ID);
            if (!panel) return;
            applyPanelState(panel);
        });
    }

    function cleanUrl(raw) {
        try {
            const u = new URL(raw || location.href);
            const path = u.pathname.replace(/\/+$/, '');
            return `${u.origin}${path}`;
        } catch (_) {
            return String(raw || location.href).split('?')[0].split('#')[0].replace(/\/+$/, '');
        }
    }

    function isVisible(el) {
        if (!el || !(el instanceof Element)) return false;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
    }

    function usableShortText(value) {
        const text = cleanText(value).replace(/[\u200B-\u200D\u2060\uFEFF]/gu, '').trim();
        if (!text || text.length > 80) return '';
        if (/^(关注|粉丝|获赞|作品|推荐|喜欢|合集|短剧|私信|已关注|分享主页|搜索)$/u.test(text)) return '';
        if (/^抖音号\s*[:：]?/u.test(text)) return '';
        return text;
    }

    function extractDouyinId() {
        const strictPattern = /抖音号[ \t]*[:：][ \t]*([A-Za-z0-9._-]{2,64})/iu;
        const bodyText = document.body ? document.body.innerText : '';
        const bodyMatch = bodyText.match(strictPattern);
        const bodyValue = bodyMatch ? cleanText(bodyMatch[1]) : '';

        let nearbyValue = '';
        const candidates = Array.from(document.querySelectorAll('span,div,p'));
        for (const el of candidates) {
            if (!isVisible(el)) continue;
            const own = cleanText(el.innerText || el.textContent);
            if (!own.includes('抖音号')) continue;
            const neighborhood = [
                own,
                cleanText(el.parentElement?.innerText),
                cleanText(el.parentElement?.parentElement?.innerText),
                cleanText(el.nextElementSibling?.innerText || el.nextElementSibling?.textContent),
            ].join('\n');
            const match = neighborhood.match(strictPattern);
            if (match) {
                nearbyValue = cleanText(match[1]);
                break;
            }
        }

        if (bodyValue && nearbyValue && bodyValue !== nearbyValue) {
            return {
                value: '',
                source: '页面抖音号候选值不一致',
                status: 'conflict',
                candidates: [bodyValue, nearbyValue],
            };
        }
        const value = nearbyValue || bodyValue;
        if (value) {
            return {
                value,
                source: nearbyValue ? '页面资料区明确抖音号' : '页面可见明确抖音号',
                status: 'found',
                candidates: [value],
            };
        }
        return { value: '', source: '未识别', status: 'missing', candidates: [] };
    }

    function nicknameFromTitle() {
        let title = cleanText(document.title);
        if (!title) return '';
        title = title
            .replace(/\s*[-_|]\s*抖音.*$/u, '')
            .replace(/的抖音主页.*$/u, '')
            .replace(/的主页.*$/u, '')
            .trim();
        if (/^(抖音|抖音精选|Douyin)$/iu.test(title)) return '';
        return usableShortText(title);
    }

    function nicknameNearDouyinId() {
        const all = Array.from(document.querySelectorAll('span,div,p'));
        for (const label of all) {
            if (!isVisible(label)) continue;
            const labelText = cleanText(label.innerText || label.textContent);
            if (!labelText.includes('抖音号')) continue;

            let node = label;
            for (let depth = 0; depth < 5 && node; depth += 1, node = node.parentElement) {
                const heading = node.querySelector?.('h1,h2,h3,[data-e2e="user-title"],[class*="name"],[class*="nickname"]');
                const headingText = usableShortText(heading?.innerText || heading?.textContent);
                if (headingText) return headingText;

                const children = Array.from(node.children || []);
                for (const child of children) {
                    if (!isVisible(child)) continue;
                    const value = usableShortText(child.innerText || child.textContent);
                    if (!value || value.includes('抖音号')) continue;
                    // Avoid selecting the whole statistics/description block.
                    if (value.length <= 30 && !/关注|粉丝|获赞/u.test(value)) return value;
                }
            }
        }
        return '';
    }

    function extractNickname(douyinId) {
        const explicitSelectors = [
            '[data-e2e="user-title"]',
            '[data-e2e="user-name"]',
        ];
        let explicitBlank = false;
        for (const selector of explicitSelectors) {
            const elements = Array.from(document.querySelectorAll(selector));
            for (const el of elements) {
                if (!isVisible(el)) continue;
                const raw = el.innerText || el.textContent || '';
                const value = usableShortText(raw);
                if (value && !value.includes('抖音号') && !/关注|粉丝|获赞/u.test(value)) {
                    return { value, source: `页面明确昵称区 (${selector})`, status: 'found' };
                }
                const cleaned = cleanText(raw).replace(/[\u200B-\u200D\u2060\uFEFF]/gu, '').trim();
                if (!cleaned) explicitBlank = true;
            }
        }

        // 只有页面上明确存在空白昵称节点，且抖音号已经识别成功时，才允许空白昵称。
        if (explicitBlank && douyinId.value && document.readyState === 'complete') {
            return { value: '', source: '页面明确昵称区为空白', status: 'blank' };
        }

        const selectorCandidates = [
            'h1',
            'h2',
            '[class*="user-info"] [class*="name"]',
            '[class*="userInfo"] [class*="name"]',
            '[class*="nickname"]',
        ];
        for (const selector of selectorCandidates) {
            const elements = Array.from(document.querySelectorAll(selector));
            for (const el of elements) {
                if (!isVisible(el)) continue;
                const value = usableShortText(el.innerText || el.textContent);
                if (value && !value.includes('抖音号') && !/关注|粉丝|获赞/u.test(value)) {
                    return { value, source: `页面可见资料区 (${selector})`, status: 'found' };
                }
            }
        }

        const near = nicknameNearDouyinId();
        if (near) return { value: near, source: '抖音号附近的页面昵称', status: 'found' };

        const fromTitle = nicknameFromTitle();
        if (fromTitle) return { value: fromTitle, source: '页面标题昵称', status: 'found' };

        return { value: '', source: '未识别', status: 'missing' };
    }

    function recognize() {
        const url = cleanUrl(location.href);
        const douyinId = extractDouyinId();
        const nickname = extractNickname(douyinId);
        const nicknameBlank = nickname.status === 'blank';
        return {
            url,
            nickname: nickname.value,
            nickname_blank: nicknameBlank,
            nickname_status: nickname.status,
            douyin_id: douyinId.value,
            douyin_id_status: douyinId.status,
            douyin_id_candidates: douyinId.candidates,
            source: `${nickname.source} + ${douyinId.source}`,
            identity: `${nickname.value}${douyinId.value}`,
        };
    }

    function profileIsComplete(profile) {
        return Boolean((profile.nickname || profile.nickname_blank) && profile.douyin_id);
    }

    function createPanel() {
        const old = document.getElementById(PANEL_ID);
        if (old) return old;

        const panel = document.createElement('div');
        panel.id = PANEL_ID;
        panel.innerHTML = `
            <div class="douk-head">
                <strong>DouK 账号采集器</strong>
                <div class="douk-head-actions">
                    <button id="douk-theme" title="切换黑白主题">主题</button>
                    <button id="douk-font-size" title="切换面板字号">A</button>
                    <button id="douk-reset-layout" title="恢复默认位置和大小">复位</button>
                    <button id="douk-minimize" title="收起/展开">-</button>
                </div>
            </div>
            <div id="douk-body">
                <div class="douk-scroll">
                    <div class="douk-row"><label>昵称</label><div class="douk-nickname-area">
                        <span id="douk-nickname">识别中...</span>
                        <span class="douk-category-controls">
                            <button class="douk-category" data-category="顶级" disabled>顶级</button>
                            <button class="douk-category" data-category="次顶级" disabled>次顶级</button>
                            <button class="douk-category" data-category="普通" disabled>普通</button>
                            <span id="douk-category-info" title="正在检查分类状态">ⓘ</span>
                        </span>
                    </div></div>
                    <div class="douk-row"><label>抖音号</label><div id="douk-id">识别中...</div></div>
                    <div class="douk-row"><label>来源</label><div id="douk-source">-</div></div>
                    <div class="douk-row douk-edit"><label>mark</label><input id="douk-mark" autocomplete="off"></div>
                    <div class="douk-row"><label>url</label><textarea id="douk-url" rows="3" readonly title="自动从当前抖音主页地址生成，已删除“?”后的查询参数和“#”锚点。此处只读，避免误修改。"></textarea></div>
                    <div id="douk-status" class="douk-status wait">正在识别并预检...</div>
                </div>
                <div class="douk-footer">
                    <div class="douk-actions">
                        <button id="douk-refresh">重新识别</button>
                        <button id="douk-add" class="primary" disabled>同时添加 JSON + Excel</button>
                        <button id="douk-screenshot" class="douk-screenshot" disabled>手动补截当前账号</button>
                    </div>
                    <div class="douk-shortcut">v${VERSION} 严格模式 · 快捷键: Alt + A</div>
                </div>
            </div>
            <div class="douk-resize douk-resize-n" data-resize="n"></div>
            <div class="douk-resize douk-resize-e" data-resize="e"></div>
            <div class="douk-resize douk-resize-s" data-resize="s"></div>
            <div class="douk-resize douk-resize-w" data-resize="w"></div>
            <div class="douk-resize douk-resize-ne" data-resize="ne"></div>
            <div class="douk-resize douk-resize-se" data-resize="se"></div>
            <div class="douk-resize douk-resize-sw" data-resize="sw"></div>
            <div class="douk-resize douk-resize-nw" data-resize="nw"></div>
        `;

        const style = document.createElement('style');
        style.textContent = `
            #${PANEL_ID}{
                --douk-bg:#17191d;
                --douk-surface:#22252b;
                --douk-surface-2:#2d3139;
                --douk-border:#3a3f49;
                --douk-border-2:#454b57;
                --douk-text:#eceff4;
                --douk-muted:#9ea7b5;
                --douk-input-bg:#0f1114;
                --douk-wait-bg:#2c3038;
                --douk-wait-text:#c8ced8;
                --douk-ok-bg:#173c28;
                --douk-ok-border:#286a43;
                --douk-ok-text:#a8efbd;
                --douk-warning-bg:#493817;
                --douk-warning-border:#8f6b24;
                --douk-warning-text:#ffe29a;
                --douk-error-bg:#4a1d25;
                --douk-error-border:#8a3445;
                --douk-error-text:#ffb9c5;
                --douk-primary:#d92d58;
                --douk-primary-border:#ed4c73;
                --douk-number-accent:#ff5f7d;
                --douk-font-base:13px;
                --douk-font-control:12px;
                --douk-font-title:14px;
                --douk-font-small:11px;
                position:fixed;
                left:calc(100vw - 374px);
                top:calc(100vh - 620px);
                width:360px;
                min-width:${MIN_PANEL_WIDTH}px;
                min-height:${MIN_PANEL_HEIGHT}px;
                display:flex;
                flex-direction:column;
                z-index:2147483647;
                background:var(--douk-bg);
                color:var(--douk-text);
                border:1px solid var(--douk-border);
                border-radius:10px;
                box-shadow:0 12px 34px rgba(0,0,0,.45);
                font:var(--douk-font-base)/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
                overflow:hidden;
            }
            #${PANEL_ID}[data-font-size="large"]{
                --douk-font-base:15px;
                --douk-font-control:14px;
                --douk-font-title:16px;
                --douk-font-small:13px;
            }
            #${PANEL_ID}[data-font-size="xlarge"]{
                --douk-font-base:17px;
                --douk-font-control:16px;
                --douk-font-title:18px;
                --douk-font-small:15px;
            }
            #${PANEL_ID}[data-theme="light"]{
                --douk-bg:#ffffff;
                --douk-surface:#f3f5f8;
                --douk-surface-2:#e7ebf1;
                --douk-border:#cfd6df;
                --douk-border-2:#bcc6d2;
                --douk-text:#1f2937;
                --douk-muted:#5f6b7b;
                --douk-input-bg:#ffffff;
                --douk-wait-bg:#eef2f7;
                --douk-wait-text:#334155;
                --douk-ok-bg:#ebf8ef;
                --douk-ok-border:#7bc495;
                --douk-ok-text:#22543d;
                --douk-warning-bg:#fff8e1;
                --douk-warning-border:#ddb84f;
                --douk-warning-text:#795200;
                --douk-error-bg:#fff0f2;
                --douk-error-border:#f2a3b3;
                --douk-error-text:#8a2741;
                --douk-number-accent:#c81842;
                box-shadow:0 12px 34px rgba(15,23,42,.16);
            }
            #${PANEL_ID} *{box-sizing:border-box}
            #${PANEL_ID} .douk-head{display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--douk-surface);border-bottom:1px solid var(--douk-border);cursor:move;user-select:none}
            #${PANEL_ID} .douk-head strong{font-size:var(--douk-font-title);flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
            #${PANEL_ID} .douk-head span{font-size:var(--douk-font-small);color:var(--douk-muted);white-space:nowrap}
            #${PANEL_ID} .douk-head-actions{display:flex;gap:6px;flex:none}
            #${PANEL_ID} .douk-head button{min-width:26px;height:24px;border:1px solid var(--douk-border);border-radius:5px;background:var(--douk-surface-2);color:var(--douk-text);cursor:pointer;padding:0 8px;font:var(--douk-font-control)/1 "Segoe UI","Microsoft YaHei",sans-serif}
            #${PANEL_ID} #douk-font-size{padding:0 6px;font-weight:700}
            #${PANEL_ID} .douk-head button:disabled{opacity:.45;cursor:not-allowed}
            #${PANEL_ID} #douk-body{padding:0;flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column}
            #${PANEL_ID} .douk-scroll{flex:1;min-height:0;overflow:auto;padding:10px 12px 8px}
            #${PANEL_ID} .douk-footer{flex:none;padding:8px 12px 12px;background:var(--douk-bg);border-top:1px solid var(--douk-border)}
            #${PANEL_ID} .douk-row{display:grid;grid-template-columns:52px 1fr;gap:8px;margin:6px 0;align-items:start}
            #${PANEL_ID} .douk-row label{color:var(--douk-muted);padding-top:3px}
            #${PANEL_ID} .douk-row>div{word-break:break-all;min-height:22px;padding:2px 0}
            #${PANEL_ID} .douk-nickname-area{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
            #${PANEL_ID} #douk-nickname{font-weight:600;min-width:0}
            #${PANEL_ID} .douk-category-controls{display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap}
            #${PANEL_ID} .douk-category{height:25px;border:1px solid var(--douk-border);border-radius:5px;background:var(--douk-surface-2);color:var(--douk-text);padding:0 7px;cursor:pointer;font:600 var(--douk-font-small)/1 "Segoe UI","Microsoft YaHei",sans-serif}
            #${PANEL_ID} .douk-category[data-selected="true"]{background:var(--douk-ok-bg);border-color:var(--douk-ok-border);color:var(--douk-ok-text)}
            #${PANEL_ID} .douk-category:disabled{opacity:.42;cursor:not-allowed}
            #${PANEL_ID} #douk-category-info{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:1px solid var(--douk-border);border-radius:50%;color:var(--douk-muted);cursor:help;font-weight:700;user-select:none}
            #${PANEL_ID} input,#${PANEL_ID} textarea{width:100%;border:1px solid var(--douk-border-2);background:var(--douk-input-bg);color:var(--douk-text);border-radius:6px;padding:7px 8px;outline:none;resize:none;font:var(--douk-font-control)/1.45 "Segoe UI","Microsoft YaHei",sans-serif}
            #${PANEL_ID} input:focus,#${PANEL_ID} textarea:focus{border-color:#e83e63}
            #${PANEL_ID} #douk-url{font:var(--douk-font-control)/1.45 "Segoe UI","Microsoft YaHei",sans-serif;min-height:78px}
            #${PANEL_ID} .douk-status{margin:8px 0 0;padding:9px;border-radius:7px;white-space:pre-wrap;word-break:break-word}
            #${PANEL_ID} .douk-status.wait{background:var(--douk-wait-bg);color:var(--douk-wait-text)}
            #${PANEL_ID} .douk-status.ok{background:var(--douk-ok-bg);color:var(--douk-ok-text);border:1px solid var(--douk-ok-border)}
            #${PANEL_ID} .douk-status.warning{background:var(--douk-warning-bg);color:var(--douk-warning-text);border:1px solid var(--douk-warning-border)}
            #${PANEL_ID} .douk-status.error{background:var(--douk-error-bg);color:var(--douk-error-text);border:1px solid var(--douk-error-border)}
            #${PANEL_ID} .douk-status-structured{white-space:normal}
            #${PANEL_ID} .douk-status-title{font-weight:700;margin-bottom:8px}
            #${PANEL_ID} .douk-ready-line{display:flex;align-items:baseline;gap:5px;margin:2px 0 9px}
            #${PANEL_ID} .douk-ready-number{color:var(--douk-number-accent);font-size:calc(var(--douk-font-title) + 2px);font-weight:800}
            #${PANEL_ID} .douk-status-line{margin:3px 0;word-break:break-word}
            #${PANEL_ID} .douk-status-help{cursor:help}
            #${PANEL_ID} .douk-status-help .douk-status-label{text-decoration:underline dotted;text-underline-offset:3px}
            #${PANEL_ID} .douk-actions{display:grid;grid-template-columns:1fr 1fr;gap:7px}
            #${PANEL_ID} .douk-actions button{min-height:36px;border:1px solid var(--douk-border);background:var(--douk-surface-2);color:var(--douk-text);border-radius:6px;padding:7px 4px;cursor:pointer;white-space:normal;font:600 var(--douk-font-control)/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
            #${PANEL_ID} .douk-actions button.primary{background:var(--douk-primary);border-color:var(--douk-primary-border);color:#fff}
            #${PANEL_ID} .douk-actions button:disabled{opacity:.45;cursor:not-allowed}
            #${PANEL_ID} .douk-actions .douk-screenshot{grid-column:1 / -1}
            #${PANEL_ID} .douk-shortcut{text-align:right;color:var(--douk-muted);font-size:var(--douk-font-small);margin-top:7px}
            #${PANEL_ID} .douk-resize{position:absolute;z-index:2}
            #${PANEL_ID}[data-minimized="true"]{min-height:0}
            #${PANEL_ID}[data-minimized="true"] .douk-resize{display:none}
            #${PANEL_ID} .douk-resize-n{top:-4px;left:10px;right:10px;height:8px;cursor:n-resize}
            #${PANEL_ID} .douk-resize-e{top:10px;right:-4px;bottom:10px;width:8px;cursor:e-resize}
            #${PANEL_ID} .douk-resize-s{left:10px;right:10px;bottom:-4px;height:8px;cursor:s-resize}
            #${PANEL_ID} .douk-resize-w{top:10px;left:-4px;bottom:10px;width:8px;cursor:w-resize}
            #${PANEL_ID} .douk-resize-ne{top:-4px;right:-4px;width:12px;height:12px;cursor:ne-resize}
            #${PANEL_ID} .douk-resize-se{right:-4px;bottom:-4px;width:12px;height:12px;cursor:se-resize}
            #${PANEL_ID} .douk-resize-sw{left:-4px;bottom:-4px;width:12px;height:12px;cursor:sw-resize}
            #${PANEL_ID} .douk-resize-nw{top:-4px;left:-4px;width:12px;height:12px;cursor:nw-resize}
        `;
        document.documentElement.appendChild(style);
        document.body.appendChild(panel);

        defaultPanelState = getDefaultPanelState(panel);
        if (panelState.left === null || panelState.top === null || panelState.width === null || panelState.height === null) {
            panelState = {
                ...defaultPanelState,
                theme: panelState.theme,
                minimized: panelState.minimized,
                docked: panelState.docked ?? true,
                fontSize: normalizePanelFontSize(panelState.fontSize),
            };
        }
        applyPanelState(panel);
        bindPanelWindowEvents();

        panel.querySelector('.douk-head').addEventListener('mousedown', (event) => startDrag(panel, event));
        panel.querySelectorAll('.douk-resize').forEach((handle) => {
            handle.addEventListener('mousedown', (event) => startResize(panel, handle, event));
        });
        panel.querySelector('#douk-theme').addEventListener('click', () => togglePanelTheme(panel));
        panel.querySelector('#douk-font-size').addEventListener('click', () => cyclePanelFontSize(panel));
        panel.querySelector('#douk-reset-layout').addEventListener('click', () => resetPanelState(panel));
        panel.querySelector('#douk-minimize').addEventListener('click', () => {
            const minimized = panel.dataset.minimized !== 'true';
            setPanelMinimized(panel, minimized);
        });
        panel.querySelector('#douk-refresh').addEventListener('click', () => recognizeAndPreview(true));
        panel.querySelector('#douk-add').addEventListener('click', addCurrent);
        panel.querySelector('#douk-screenshot').addEventListener('click', captureCurrentAccountScreenshot);
        panel.querySelectorAll('.douk-category').forEach((button) => {
            button.addEventListener('click', () => classifyCurrent(button.dataset.category));
        });
        panel.querySelector('#douk-mark').addEventListener('input', () => {
            markEditedByUser = true;
        });
        return panel;
    }

    function panelEl(selector) {
        return createPanel().querySelector(selector);
    }

    function setStatus(kind, message) {
        const el = panelEl('#douk-status');
        el.className = `douk-status ${kind}`;
        el.textContent = message;
    }

    function disconnectedCategoryState(message = '未连接本机服务。请在 DouK 管理器“账号采集”页启动服务，然后刷新页面或点击“重新识别”。') {
        return {
            available: false,
            status: 'disconnected',
            category: '',
            categories: [],
            message,
            guidance: message,
        };
    }

    function setCategoryState(state) {
        currentCategoryState = state && typeof state === 'object'
            ? state
            : disconnectedCategoryState();

        const status = currentCategoryState.status || 'unavailable';
        const selected = cleanText(currentCategoryState.category);
        const buttons = Array.from(createPanel().querySelectorAll('.douk-category'));
        const info = panelEl('#douk-category-info');
        const help = cleanText(currentCategoryState.guidance)
            || cleanText(currentCategoryState.message)
            || '分类状态不可用。';
        info.title = help;

        for (const button of buttons) {
            const category = button.dataset.category;
            const isSelected = status === 'classified' && category === selected;
            button.textContent = `${isSelected ? '✓ ' : ''}${category}`;
            button.dataset.selected = isSelected ? 'true' : 'false';

            if (isClassifying || isAdding) {
                button.disabled = true;
            } else if (status === 'unclassified' && currentCategoryState.available === true) {
                button.disabled = false;
            } else if (status === 'classified') {
                button.disabled = !isSelected;
            } else {
                button.disabled = true;
            }
            button.title = isSelected
                ? help
                : (button.disabled ? help : `将该账号记录到“${category}.txt”`);
        }
    }

    function disableCategoriesForConnection(message) {
        setCategoryState(disconnectedCategoryState(message));
    }

    function appendStatusLine(container, label, value, title = '') {
        const line = document.createElement('div');
        line.className = `douk-status-line${title ? ' douk-status-help' : ''}`;
        if (title) line.title = title;

        const labelElement = document.createElement('span');
        labelElement.className = 'douk-status-label';
        labelElement.textContent = `${label}${title ? ' ⓘ' : ''}：`;
        line.appendChild(labelElement);
        line.appendChild(document.createTextNode(String(value ?? '')));
        container.appendChild(line);
    }

    function setPreviewSuccessStatus(data) {
        const el = panelEl('#douk-status');
        el.className = 'douk-status ok douk-status-structured';
        el.replaceChildren();

        const numberText = cleanText(data.number_text);
        const numberMatch = numberText.match(/^A(\d+)$/i);
        const number = numberMatch ? Number(numberMatch[1]) : null;
        const hasNumber = Number.isSafeInteger(number);

        const title = document.createElement('div');
        title.className = 'douk-status-title';
        title.textContent = '✅ 严格预检通过';
        el.appendChild(title);

        if (data.nickname_blank) {
            appendStatusLine(
                el,
                '空白昵称',
                '已确认，mark 将仅使用 A 编号＋抖音号',
                '页面上存在明确的昵称区域，但内容清理后为空；这与页面尚未识别到昵称不同。'
            );
        }

        const readyLine = document.createElement('div');
        readyLine.className = 'douk-ready-line douk-status-help';
        readyLine.title = `点击“同时添加 JSON + Excel”后，两份文件将共同使用新编号 ${numberText}。`;
        const readyLabel = document.createElement('span');
        readyLabel.className = 'douk-status-label';
        readyLabel.textContent = '准备写入 ⓘ：';
        const readyNumber = document.createElement('strong');
        readyNumber.className = 'douk-ready-number';
        readyNumber.textContent = numberText;
        readyLine.append(readyLabel, readyNumber);
        el.appendChild(readyLine);

        appendStatusLine(
            el,
            '一致性',
            `JSON ${data.json_max} / Excel ${data.excel_max}`,
            `当前已存在的最后编号：JSON 为 ${data.json_max}，Excel 为 ${data.excel_max}。两者一致，下一条将使用 ${numberText}。`
        );
        appendStatusLine(
            el,
            'JSON 位置',
            data.json_position,
            hasNumber
                ? `JSON 数组索引从 0 开始，因此 ${numberText} 的数组下标为 ${number}−1＝${number - 1}，将写入 ${data.json_position}。`
                : `本条记录将写入 JSON 的 ${data.json_position}。`
        );
        appendStatusLine(
            el,
            'Excel 位置',
            `${data.excel_name_cell} / ${data.excel_url_cell}`,
            hasNumber
                ? `Excel 第 1 行对应 A0，因此 ${numberText} 的行号为 ${number}＋1＝${number + 1}；名称写入 B${number + 1}:C${number + 1}，URL 写入 D${number + 1}:J${number + 1}。`
                : `名称写入 ${data.excel_name_cell}，URL 写入 ${data.excel_url_cell}。`
        );
        appendStatusLine(
            el,
            '历史备份',
            data.history_backup_due_after_add
                ? '本次成功后生成一组'
                : `本次成功后 ${data.history_backup_progress_after_add}/${data.history_backup_interval}`
        );
    }

    function setAddEnabled(enabled) {
        panelEl('#douk-add').disabled = !enabled || isAdding;
    }

    function setScreenshotEnabled(enabled, title = '') {
        screenshotReady = Boolean(enabled);
        const button = panelEl('#douk-screenshot');
        button.disabled = !screenshotReady || isAdding || isClassifying || !pageIsVisible();
        button.title = title || (screenshotReady
            ? '按当前 URL 重新核验 JSON、Excel 后补截，不覆盖已有图片。'
            : '当前账号尚未通过截图核验。');
    }

    async function refreshScreenshotReadiness(profileUrl) {
        setScreenshotEnabled(false, '正在核验 JSON、Excel、URL 和截图编号…');
        if (!pageIsVisible() || !profileUrl) return false;
        try {
            const currentUrl = recognize().url;
            const result = await api('/account-screenshot-check', {
                expected_url: profileUrl,
                current_url: currentUrl,
            });
            if (result.ok && recognize().url === profileUrl && pageIsVisible()) {
                setScreenshotEnabled(true, `${result.data.number_text}.jpg 已通过 JSON＋Excel 严格核验，可手动补截。`);
                return true;
            }
            setScreenshotEnabled(false, result.message || '当前账号未通过截图核验。');
            return false;
        } catch (error) {
            setScreenshotEnabled(false, error.message || '截图核验失败。');
            return false;
        }
    }

    async function captureCurrentAccountScreenshot() {
        if (!screenshotReady || isAdding || isClassifying) return;
        if (!pageIsVisible()) {
            setScreenshotEnabled(false, '后台标签页禁止截图。');
            setStatus('warning', '⚠️ 后台标签页禁止截图，请先切回当前标签页。');
            return;
        }
        const currentUrl = recognize().url;
        if (!currentUrl) {
            setScreenshotEnabled(false, '当前主页 URL 无效。');
            return;
        }

        setScreenshotEnabled(false, '截图处理中…');
        setStatus('wait', '正在重新核验 JSON、Excel、URL 和编号，然后保存手动补截图…');
        try {
            const result = await api('/account-screenshot', {
                expected_url: currentUrl,
                current_url: currentUrl,
            });
            if (!result.ok) {
                const details = formatDetails(result.details);
                setStatus('warning', `⚠️ 手动截图失败：${result.message}\n错误代码：${result.code}${details ? `\n\n${details}` : ''}\n本次没有保存或覆盖任何图片。`);
                return;
            }
            setStatus('ok', `✅ 手动补截成功：${result.data.number_text}.jpg\n截图位置：${result.data.relative_path}\n已有同名图片时绝不覆盖。`);
        } catch (error) {
            setStatus('warning', `⚠️ 手动截图失败：${error.message}\n本次没有保存或覆盖任何图片。`);
        }
    }

    function formatDetails(details) {
        if (!details || typeof details !== 'object') return '';
        const aliases = {
            duplicate_type: '重复类型', existing_mark: '已有 mark', existing_url: '已有 URL',
            classification_status: '分类状态',
            json_position: 'JSON 位置', json_index: 'JSON 索引', mark: 'mark', url: 'url',
            row: 'Excel 行', number: '编号', b_cell: '名称单元格', b_value: '名称内容',
            d_cell: 'URL 单元格', d_value: 'URL 内容', hyperlink: '残留超链接', action: '处理方法',
            json_max: 'JSON 最大编号', excel_max: 'Excel 最大编号', missing_numbers: '缺失编号',
            only_in_json: '仅 JSON 存在', only_in_excel: '仅 Excel 存在', error: '错误详情',
        };
        const rawMessageKeys = new Set(['classification_conflict', 'classification_guidance']);
        const hiddenKeys = new Set(['category_state']);
        const preferredOrder = [
            'duplicate_type',
            'classification_status',
            'existing_mark',
            'existing_url',
            'json_position',
            'json_index',
        ];
        const entries = Object.entries(details)
            .filter(([key, value]) => !hiddenKeys.has(key) && value !== '' && value !== null && value !== undefined)
            .sort(([left], [right]) => {
                const leftIndex = preferredOrder.indexOf(left);
                const rightIndex = preferredOrder.indexOf(right);
                if (leftIndex >= 0 || rightIndex >= 0) {
                    return (leftIndex >= 0 ? leftIndex : preferredOrder.length)
                        - (rightIndex >= 0 ? rightIndex : preferredOrder.length);
                }
                const leftRaw = rawMessageKeys.has(left) ? 1 : 0;
                const rightRaw = rawMessageKeys.has(right) ? 1 : 0;
                return leftRaw - rightRaw;
            });
        return entries
            .map(([key, value]) => {
                const shown = Array.isArray(value) ? value.join(', ') : (typeof value === 'object' ? JSON.stringify(value) : String(value));
                if (rawMessageKeys.has(key)) return shown;
                return `${aliases[key] || key}：${shown}`;
            })
            .join('\n');
    }

    function connectionError(message) {
        const error = new Error(message);
        error.isConnectionFailure = true;
        return error;
    }

    function wait(milliseconds) {
        return new Promise((resolve) => setTimeout(resolve, milliseconds));
    }

    function api(path, payload) {
        return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${API_BASE}${path}`,
                headers: {
                    'Content-Type': 'application/json',
                    'X-DouK-Token': TOKEN,
                },
                data: JSON.stringify({
                    ...payload,
                    client_version: VERSION,
                    page_visible: pageIsVisible(),
                    tab_id: TAB_ID,
                }),
                timeout: 20000,
                onload: (response) => {
                    try {
                        resolve(JSON.parse(response.responseText));
                    } catch (error) {
                        reject(new Error(`服务返回内容无法解析：${error.message}`));
                    }
                },
                ontimeout: () => reject(connectionError('连接本机服务超时，请确认 DouK 管理器中的账号采集服务仍在运行。')),
                onerror: () => reject(connectionError('无法访问 127.0.0.1:8765，请在 DouK 管理器“账号采集”页启动服务。')),
            });
        });
    }

    function formatAddResultStatus(addData, screenshotLine, addDetails, screenshotDetails = '') {
        const blocks = [
            `✅ JSON＋Excel 写入成功：${addData.number_text}`,
            screenshotLine,
            '',
            addDetails,
        ];
        if (screenshotDetails) blocks.push('', screenshotDetails);
        return blocks.join('\n');
    }

    async function captureAddedAccountScreenshot(addData, addDetails) {
        try {
            // Let Chrome paint the successful A-number message before capture.
            await wait(350);
            if (!pageIsVisible()) {
                throw new Error('截图前标签页已进入后台，本次截图未保存。');
            }

            const currentUrl = recognize().url;
            if (currentUrl !== addData.url) {
                throw new Error('截图前页面已经切换，本次截图未保存。');
            }

            const result = await api('/account-screenshot', {
                number_text: addData.number_text,
                expected_url: addData.url,
                current_url: currentUrl,
            });
            if (!result.ok) {
                const details = formatDetails(result.details);
                setStatus(
                    'warning',
                    formatAddResultStatus(
                        addData,
                        `⚠️ 截图失败：${result.message}`,
                        addDetails,
                        `错误代码：${result.code}${details ? `\n${details}` : ''}\nJSON 和 Excel 已成功写入，不会因截图失败而回滚。`
                    )
                );
                // A capture/foreground failure can be retried manually, but
                // mapping conflicts and an existing file remain disabled.
                if (result.code !== 'SCREENSHOT_ALREADY_EXISTS'
                    && result.code !== 'SCREENSHOT_ACCOUNT_MISMATCH'
                    && result.code !== 'SCREENSHOT_JSON_EXCEL_MISMATCH') {
                    await refreshScreenshotReadiness(addData.url);
                }
                return;
            }

            setStatus(
                'ok',
                formatAddResultStatus(
                    addData,
                    `✅ 截图成功：${addData.number_text}.jpg`,
                    addDetails,
                    `截图位置：${result.data.relative_path}\n尺寸：${result.data.width} × ${result.data.height}\n已有同名图片时绝不覆盖。`
                )
            );
            setScreenshotEnabled(false, `${addData.number_text}.jpg 已保存，不覆盖已有图片。`);
        } catch (error) {
            setStatus(
                'warning',
                formatAddResultStatus(
                    addData,
                    `⚠️ 截图失败：${error.message}`,
                    addDetails,
                    'JSON 和 Excel 已成功写入，不会因截图失败而回滚。'
                )
            );
            await refreshScreenshotReadiness(addData.url);
        }
    }

    async function previewApiWithRetry(payload, stillCurrent) {
        let lastError = null;
        for (let attempt = 1; attempt <= PREVIEW_CONNECTION_ATTEMPTS; attempt += 1) {
            if (attempt > 1 && !stillCurrent()) throw lastError;
            try {
                return await api('/preview', payload);
            } catch (error) {
                lastError = error;
                if (!error?.isConnectionFailure || attempt >= PREVIEW_CONNECTION_ATTEMPTS || !stillCurrent()) {
                    throw error;
                }
                setStatus('wait', `本机服务连接失败，正在进行第 ${attempt + 1}/${PREVIEW_CONNECTION_ATTEMPTS} 次尝试…`);
                await wait(PREVIEW_RETRY_DELAYS[attempt - 1] || 900);
            }
        }
        throw lastError;
    }

    async function recognizeAndPreview(force = false) {
        clearTimeout(previewTimer);
        previewTimer = setTimeout(async () => {
            if (!pageIsVisible()) {
                setAddEnabled(false);
                disableCategoriesForConnection('后台标签页已暂停分类操作。');
                return;
            }
            const recognized = recognize();
            // URL 是稳定账号键；昵称/抖音号属于页面动态文本，不应用于判断是否换了账号。
            const profileKey = recognized.url;

            if (profileKey !== currentProfileKey) {
                currentProfileKey = profileKey;
                lastCompletedProfileKey = '';
                markEditedByUser = false;
                panelEl('#douk-mark').value = '';
            }

            // 自动检测对同一 URL 只执行一次，也不覆盖已经显示的按钮状态。
            if (!force && lastCompletedProfileKey === profileKey) return;

            panelEl('#douk-nickname').textContent = recognized.nickname_blank
                ? '（空白昵称）'
                : (recognized.nickname || '未识别');
            panelEl('#douk-id').textContent = recognized.douyin_id || '未识别';
            panelEl('#douk-source').textContent = recognized.source;
            panelEl('#douk-url').value = recognized.url;
            setAddEnabled(false);
            setScreenshotEnabled(false, '正在读取当前账号状态…');
            setCategoryState({
                available: false,
                status: 'checking',
                guidance: '正在读取本机分类状态…',
            });

            // 页面可能仍在加载，所以识别不完整时不缓存完成状态。
            // 后续抖音页面出现新的外部 DOM 变化时会自动再识别；手动按钮也可强制再检测。
            if (!profileIsComplete(recognized)) {
                const reason = recognized.douyin_id_status === 'conflict'
                    ? `页面中识别到不一致的抖音号候选值：${(recognized.douyin_id_candidates || []).join(' / ')}。`
                    : (!recognized.douyin_id
                        ? '抖音号缺失或未能明确识别。'
                        : '未找到可确认的昵称内容，也未确认为空白昵称。');
                setStatus('error', `⛔ 当前主页资料未识别完整\n\n${reason}\n请等待页面加载后点击“重新识别”，不要添加。`);
                setCategoryState({
                    available: false,
                    status: 'unavailable',
                    guidance: `${reason}暂时不能分类。`,
                });
                return;
            }

            // 同一时刻只允许一个 /preview。请求期间发生页面切换或手动刷新时，只排队一次。
            if (previewRequestInFlight) {
                if (force || profileKey !== activePreviewKey) {
                    queuedPreview = true;
                    queuedForcePreview = queuedForcePreview || force;
                }
                return;
            }

            previewRequestInFlight = true;
            activePreviewKey = profileKey;
            const requestSerial = ++previewRequestSerial;
            const requestProfileKey = profileKey;

            setStatus('wait', '正在同时读取 settings_master.json 和 Excel，并执行严格预检…');
            try {
                const previewPayload = {
                    nickname: recognized.nickname,
                    nickname_blank: recognized.nickname_blank,
                    douyin_id: recognized.douyin_id,
                    url: recognized.url,
                    mark: markEditedByUser ? panelEl('#douk-mark').value : '',
                };
                const result = await previewApiWithRetry(
                    previewPayload,
                    () => requestSerial === previewRequestSerial
                        && recognize().url === requestProfileKey
                        && pageIsVisible()
                );

                // 页面已切换时，旧账号的响应不能覆盖新账号面板。
                const latestProfileKey = recognize().url;
                if (requestSerial !== previewRequestSerial || latestProfileKey !== requestProfileKey || !pageIsVisible()) {
                    if (pageIsVisible()) queuedPreview = true;
                    return;
                }

                lastCompletedProfileKey = requestProfileKey;

                if (!result.ok) {
                    const details = formatDetails(result.details);
                    setStatus('error', `⛔ ${result.message}\n错误代码：${result.code}${details ? `\n\n${details}` : ''}\n\n本次未写入 JSON，也未写入 Excel。`);
                    setCategoryState(result.details?.category_state || {
                        available: false,
                        status: 'unavailable',
                        guidance: '当前预检错误不允许分类操作。',
                    });
                    setAddEnabled(false);
                    if (result.code === 'DUPLICATE_JSON_URL') {
                        await refreshScreenshotReadiness(requestProfileKey);
                    } else {
                        setScreenshotEnabled(false, result.message || '当前账号未通过截图核验。');
                    }
                    return;
                }

                // 保持与当前 collector_server.py 的嵌套 data 返回格式一致。
                if (!markEditedByUser) panelEl('#douk-mark').value = result.data.mark;
                panelEl('#douk-url').value = result.data.url;
                setPreviewSuccessStatus(result.data);
                setCategoryState(result.data.category_state);
                setAddEnabled(true);
                setScreenshotEnabled(false, '当前账号尚未写入 JSON，添加成功后才可截图。');
            } catch (error) {
                // 仅缓存本次 URL 的连接错误；手动“重新识别”仍可立即重试。
                if (recognize().url !== requestProfileKey || !pageIsVisible()) {
                    if (pageIsVisible()) queuedPreview = true;
                    return;
                }
                lastCompletedProfileKey = requestProfileKey;
                setStatus('error', `⛔ ${error.message}\n\n油猴脚本已运行，但本机 Python 服务未连接。点击“重新识别”可再次尝试。`);
                disableCategoriesForConnection(error.message);
                setAddEnabled(false);
            } finally {
                // 无论响应是否过期，都必须释放请求锁，避免页面切换时永久卡死。
                previewRequestInFlight = false;
                activePreviewKey = '';

                if (queuedPreview && pageIsVisible()) {
                    const forceNext = queuedForcePreview;
                    queuedPreview = false;
                    queuedForcePreview = false;
                    recognizeAndPreview(forceNext);
                } else if (!pageIsVisible()) {
                    queuedPreview = false;
                    queuedForcePreview = false;
                }
            }
        }, force ? 0 : 650);
    }

    async function addCurrent() {
        if (isAdding) return;
        if (!pageIsVisible()) {
            setStatus('error', '⛔ 后台标签页禁止写入。请先切换到该标签页再操作。');
            return;
        }
        const recognized = recognize();
        if (!profileIsComplete(recognized)) {
            setStatus('error', '⛔ 昵称或抖音号未识别完整，已停止添加。');
            return;
        }

        isAdding = true;
        setAddEnabled(false);
        setScreenshotEnabled(false, '正在添加账号，暂不能截图…');
        setCategoryState(currentCategoryState);
        setStatus('wait', '正在重新读取 JSON 和 Excel 并执行最终检查…\n通过后才会同时写入两份文件。');
        try {
            const result = await api('/add', {
                nickname: recognized.nickname,
                nickname_blank: recognized.nickname_blank,
                douyin_id: recognized.douyin_id,
                url: recognized.url,
                mark: panelEl('#douk-mark').value,
            });
            if (!result.ok) {
                const details = formatDetails(result.details);
                setStatus('error', `⛔ ${result.message}\n错误代码：${result.code}${details ? `\n\n${details}` : ''}\n\n本次未写入 JSON，也未写入 Excel。`);
                if (result.details?.category_state) setCategoryState(result.details.category_state);
                return;
            }

            panelEl('#douk-mark').value = result.data.mark;
            panelEl('#douk-url').value = result.data.url;
            setCategoryState(result.data.category_state);
            const backupStatus = result.data.history_backup_created
                ? `历史备份：已生成第 20 次 JSON＋Excel 快照`
                : `历史备份进度：${result.data.history_backup_progress}/${result.data.history_backup_interval}`;
            const addDetails = `mark：${result.data.mark}${result.data.nickname_blank ? '\n昵称：已确认为空白，mark 仅使用抖音号' : ''}\nurl：${result.data.url}\nJSON：${result.data.json_position}\nExcel：${result.data.excel_name_cell} / ${result.data.excel_url_cell}\n即时备份：两个 .bak 已覆盖更新\n${backupStatus}`;
            setStatus(
                'ok',
                formatAddResultStatus(
                    result.data,
                    `⏳ 正在保存截图：${result.data.number_text}.jpg`,
                    addDetails
                )
            );
            await captureAddedAccountScreenshot(result.data, addDetails);
        } catch (error) {
            setStatus('error', `⛔ ${error.message}\n\n请查看 DouK 管理器的采集器日志。`);
            if (error?.isConnectionFailure) disableCategoriesForConnection(error.message);
        } finally {
            isAdding = false;
            setCategoryState(currentCategoryState);
            // The just-added account is now a duplicate, so do not re-enable Add on this page.
            setAddEnabled(false);
        }
    }

    async function classifyCurrent(category) {
        if (isClassifying || isAdding || !CATEGORY_NAMES.includes(category)) return;
        if (!pageIsVisible()) {
            setStatus('error', '⛔ 后台标签页禁止分类修改。请先切换到该标签页。');
            setCategoryState({
                available: false,
                status: 'unavailable',
                guidance: '后台标签页禁止分类修改。',
            });
            return;
        }

        const recognized = recognize();
        if (!profileIsComplete(recognized)) {
            setStatus('error', '⛔ 昵称或抖音号未识别完整，已停止分类。');
            return;
        }

        isClassifying = true;
        setCategoryState(currentCategoryState);
        setStatus('wait', `正在校验 settings_master.json 中的真实 A 编号，并更新“${category}.txt”…`);
        try {
            const result = await api('/classify', {
                nickname: recognized.nickname,
                nickname_blank: recognized.nickname_blank,
                douyin_id: recognized.douyin_id,
                url: recognized.url,
                category,
            });
            if (!result.ok) {
                const details = formatDetails(result.details);
                setStatus('error', `⛔ ${result.message}\n错误代码：${result.code}${details ? `\n\n${details}` : ''}\n\n本次未修改任何分类 TXT。`);
                if (result.details?.category_state) {
                    setCategoryState(result.details.category_state);
                } else if (result.code === 'CATEGORY_DATA_CONFLICT') {
                    setCategoryState({
                        available: false,
                        status: 'conflict',
                        guidance: result.message,
                    });
                } else if (result.code !== 'CATEGORY_CHANGE_REQUIRES_CANCEL') {
                    setCategoryState({
                        available: false,
                        status: 'unavailable',
                        guidance: result.message,
                    });
                }
                return;
            }

            setCategoryState(result.data.category_state);
            const categoryStatus = result.data.action === 'removed'
                ? `✅ 取消分类成功：${result.data.number_text} ← ${result.data.category}`
                : `✅ 分类成功：${result.data.number_text} → ${result.data.category}`;
            const currentClassification = result.data.category_state?.status === 'classified'
                ? result.data.category_state.category
                : '尚未分类';
            setStatus(
                'ok',
                `${categoryStatus}\n\n账号状态：已收录\n分类状态：${currentClassification}\n${result.message}\n分类文件已重新排序；只有 3 个及以上连续编号才会合并为范围。`
            );
        } catch (error) {
            setStatus('error', `⛔ ${error.message}\n\n分类 TXT 未进行自动重试。重启管理器中的采集服务后，请点击“重新识别”。`);
            if (error?.isConnectionFailure) disableCategoriesForConnection(error.message);
        } finally {
            isClassifying = false;
            setCategoryState(currentCategoryState);
        }
    }

    function scheduleRecognition() {
        if (!pageIsVisible()) return;
        if (recognizeTimer) return;
        recognizeTimer = setTimeout(() => {
            recognizeTimer = null;
            recognizeAndPreview(false);
        }, 600);
    }

    function mutationIsInsidePanel(mutation) {
        const target = mutation.target instanceof Element
            ? mutation.target
            : mutation.target?.parentElement;
        return Boolean(target?.closest?.(`#${PANEL_ID}`));
    }

    function observeSpaNavigation() {
        let lastUrl = cleanUrl(location.href);
        const check = () => {
            if (!pageIsVisible()) return;
            const normalized = cleanUrl(location.href);
            if (normalized !== lastUrl) {
                lastUrl = normalized;
                currentProfileKey = '';
                lastCompletedProfileKey = '';
                markEditedByUser = false;
                ++previewRequestSerial; // 使旧 URL 的响应失效
                if (previewRequestInFlight) queuedPreview = true;
                scheduleRecognition();
            }
        };
        setInterval(check, 700);

        const observer = new MutationObserver((mutations) => {
            if (!pageIsVisible()) return;
            if (!document.getElementById(PANEL_ID)) {
                createPanel();
                scheduleRecognition();
                return;
            }

            // 当前账号已经预检完成后，视频悬停、搜索提示等普通 DOM 变化不应禁用添加按钮。
            if (lastCompletedProfileKey === cleanUrl(location.href)) return;

            // 只响应抖音页面本身的变化；面板内部状态更新不能触发新一轮 /preview。
            if (mutations.every(mutationIsInsidePanel)) return;
            scheduleRecognition();
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
    }

    document.addEventListener('visibilitychange', () => {
        if (!pageIsVisible()) {
            clearTimeout(previewTimer);
            clearTimeout(recognizeTimer);
            recognizeTimer = null;
            ++previewRequestSerial;
            queuedPreview = false;
            queuedForcePreview = false;
            setAddEnabled(false);
            setScreenshotEnabled(false, '后台标签页已暂停截图。');
            setCategoryState({
                available: false,
                status: 'unavailable',
                guidance: '后台标签页已暂停分类操作；切回后将重新检查。',
            });
            setStatus('wait', '后台标签页已暂停检测。切回此标签页时会自动重新读取 JSON 和 Excel。');
            return;
        }

        currentProfileKey = '';
        lastCompletedProfileKey = '';
        markEditedByUser = false;
        panelState = loadPanelState();
        const panel = document.getElementById(PANEL_ID);
        if (panel) applyPanelState(panel);
        setScreenshotEnabled(false, '正在重新核验当前账号…');
        recognizeAndPreview(true);
    });

    document.addEventListener('keydown', (event) => {
        if (event.altKey && !event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === 'a') {
            if (event.target instanceof Element && event.target.closest('input,textarea,[contenteditable="true"]')) return;
            event.preventDefault();
            if (!panelEl('#douk-add').disabled) addCurrent();
        }
    }, true);

    createPanel();
    observeSpaNavigation();
    if (pageIsVisible()) {
        recognizeAndPreview(true);
    } else {
        setAddEnabled(false);
        setCategoryState({
            available: false,
            status: 'unavailable',
            guidance: '后台标签页已暂停分类操作。',
        });
        setStatus('wait', '后台标签页已暂停检测。切回此标签页时会自动检测。');
    }
})();
