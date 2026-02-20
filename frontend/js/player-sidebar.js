(function () {
    const STORAGE_KEY = 'alphy_player_meta_collapsed_v1';
    const PLACEHOLDER = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 600"><rect width="400" height="600" fill="%23111111"/><text x="200" y="320" fill="%23666" text-anchor="middle" font-size="26">No Cover</text></svg>';

    const state = {
        initialized: false,
        collapsed: false,
        item: null,
        callbacks: {},
        requestToken: 0,
        cache: new Map(),
        coverUrls: [],
        coverIndex: 0,
    };

    function node(id) {
        return document.getElementById(id);
    }

    function keyFor(item) {
        if (!item) return '';
        if (item.url) return item.url;
        if (item.type && item.id) return `${item.type}:${item.id}`;
        return `${item.type || ''}:${item.title || ''}:${item.year || ''}`;
    }

    function applyCollapsedState() {
        const shell = node('playerMetaShell');
        const toggle = node('playerMetaToggle');
        if (!shell || !toggle) return;
        shell.classList.toggle('collapsed', state.collapsed);
        toggle.textContent = state.collapsed ? '›' : '‹';
        toggle.setAttribute('aria-label', state.collapsed ? 'Show details panel' : 'Hide details panel');
    }

    function toggleCollapsed() {
        state.collapsed = !state.collapsed;
        try {
            localStorage.setItem(STORAGE_KEY, state.collapsed ? '1' : '0');
        } catch (_) {}
        applyCollapsedState();
    }

    function queryMeta(item) {
        const params = new URLSearchParams();
        if (item?.type) params.set('type', item.type);
        if (item?.id) params.set('id', String(item.id));
        if (item?.url) params.set('url', item.url);
        if (item?.title) params.set('title', item.title);
        if (item?.year) params.set('year', String(item.year));
        return `/api/soap/player-meta?${params.toString()}`;
    }

    function setRatingLink(linkEl, valueEl, data) {
        if (!linkEl || !valueEl) return;
        const value = data?.value;
        valueEl.textContent = value || '—';
        const href = data?.url;
        if (href) {
            linkEl.href = href;
            linkEl.setAttribute('target', '_blank');
            linkEl.setAttribute('rel', 'noopener noreferrer');
            linkEl.style.pointerEvents = 'auto';
        } else {
            linkEl.removeAttribute('href');
            linkEl.removeAttribute('target');
            linkEl.removeAttribute('rel');
            linkEl.style.pointerEvents = 'none';
        }
    }

    function updateBookmarkState() {
        const bookmarkBtn = node('playerMetaBookmark');
        if (!bookmarkBtn || !state.item || typeof state.callbacks.isBookmarked !== 'function') return;
        bookmarkBtn.classList.toggle('active', !!state.callbacks.isBookmarked(state.item));
    }

    function setLoadingState(loading) {
        const panel = node('playerMetaPanel');
        const shell = node('playerMetaShell');
        panel?.classList.toggle('loading', !!loading);
        shell?.classList.toggle('is-loading', !!loading);
    }

    function uniqueCoverList(list) {
        const seen = new Set();
        const out = [];
        for (const entry of list || []) {
            const value = typeof entry === 'string' ? entry.trim() : '';
            if (!value || seen.has(value)) continue;
            seen.add(value);
            out.push(value);
        }
        return out;
    }

    function setCoverList(covers, preferredCover) {
        const ordered = [];
        if (preferredCover) ordered.push(preferredCover);
        if (Array.isArray(covers)) ordered.push(...covers);
        const normalized = uniqueCoverList(ordered);
        state.coverUrls = normalized.length ? normalized : [PLACEHOLDER];
        state.coverIndex = 0;
        renderCover();
    }

    function shiftCover(delta) {
        if (!state.coverUrls.length) return;
        if (state.coverUrls.length <= 1) return;
        const total = state.coverUrls.length;
        state.coverIndex = (state.coverIndex + delta + total) % total;
        renderCover();
    }

    function renderCover() {
        const cover = node('playerMetaCover');
        const coverWrap = node('playerMetaCoverWrap');
        const prevBtn = node('playerMetaCoverPrev');
        const nextBtn = node('playerMetaCoverNext');
        const counter = node('playerMetaCoverCounter');
        if (!cover || !coverWrap || !prevBtn || !nextBtn || !counter) return;

        const total = state.coverUrls.length;
        const current = state.coverUrls[state.coverIndex] || PLACEHOLDER;
        cover.decoding = 'async';
        cover.loading = 'eager';
        cover.src = current;
        const hasMultiple = total > 1;
        coverWrap.classList.toggle('has-multiple', hasMultiple);
        counter.textContent = hasMultiple ? `${state.coverIndex + 1}/${total}` : '';
    }

    async function waitForCurrentCoverLoad(timeoutMs = 2200) {
        const cover = node('playerMetaCover');
        if (!cover) return;
        const expected = state.coverUrls[state.coverIndex] || '';
        if (!expected || expected === PLACEHOLDER) return;
        if (cover.complete && cover.naturalWidth > 0) return;

        await new Promise((resolve) => {
            let done = false;
            const finish = () => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                resolve();
            };
            const timer = setTimeout(finish, timeoutMs);
            cover.addEventListener('load', finish, { once: true });
            cover.addEventListener('error', finish, { once: true });
        });
    }

    function renderBase() {
        const panel = node('playerMetaPanel');
        const cover = node('playerMetaCover');
        const year = node('playerMetaYear');
        const description = node('playerMetaDescription');
        const ratings = node('playerMetaRatings');
        const lbxdLink = node('playerRatingLbxd');

        if (!panel || !cover || !description || !ratings || !lbxdLink || !year) return;

        cover.alt = state.item?.title || 'Cover';
        setCoverList([], null);

        year.textContent = state.item?.year ? String(state.item.year) : '';
        year.classList.toggle('empty', !year.textContent);

        description.textContent = '';
        description.classList.remove('expanded');
        description.classList.add('empty');

        const isSeries = state.item?.type === 'series';
        lbxdLink.style.display = isSeries ? 'none' : 'flex';
        ratings.classList.toggle('two-col', isSeries);

        setRatingLink(node('playerRatingImdb'), node('playerRatingImdbValue'), null);
        setRatingLink(node('playerRatingKp'), node('playerRatingKpValue'), null);
        setRatingLink(node('playerRatingLbxd'), node('playerRatingLbxdValue'), null);

        updateBookmarkState();
    }

    function renderMeta(meta) {
        const year = node('playerMetaYear');
        const description = node('playerMetaDescription');
        const ratings = node('playerMetaRatings');
        const lbxdLink = node('playerRatingLbxd');

        if (!description || !ratings || !lbxdLink || !year) return;

        setCoverList(meta?.covers, meta?.cover);
        const cover = node('playerMetaCover');
        if (cover) {
            cover.alt = (state.item?.title || meta?.title || 'Cover');
        }

        const yearText = meta?.year || state.item?.year || '';
        year.textContent = yearText ? String(yearText) : '';
        year.classList.toggle('empty', !year.textContent);

        const text = (meta?.description || '').trim();
        description.textContent = text;
        description.classList.remove('expanded');
        description.classList.toggle('empty', !text);

        setRatingLink(node('playerRatingImdb'), node('playerRatingImdbValue'), meta?.ratings?.imdb || null);
        setRatingLink(node('playerRatingKp'), node('playerRatingKpValue'), meta?.ratings?.kp || null);
        setRatingLink(node('playerRatingLbxd'), node('playerRatingLbxdValue'), meta?.ratings?.lbxd || null);

        const showLbxd = state.item?.type === 'movie';
        lbxdLink.style.display = showLbxd ? 'flex' : 'none';
        ratings.classList.toggle('two-col', !showLbxd);

        updateBookmarkState();
    }

    async function fetchAndRender(item) {
        const cacheKey = keyFor(item);
        const currentRequest = ++state.requestToken;
        setLoadingState(true);

        try {
            if (cacheKey && state.cache.has(cacheKey)) {
                renderMeta(state.cache.get(cacheKey));
            } else {
                const response = await fetch(queryMeta(item));
                if (!response.ok) throw new Error('meta fetch failed');
                const data = await response.json();
                if (currentRequest !== state.requestToken) return;
                if (cacheKey) state.cache.set(cacheKey, data || {});
                renderMeta(data || {});
            }
            if (currentRequest !== state.requestToken) return;
            await waitForCurrentCoverLoad();
        } catch (_) {
            if (currentRequest !== state.requestToken) return;
        } finally {
            if (currentRequest === state.requestToken) setLoadingState(false);
        }
    }

    function setItem(item) {
        state.item = item ? { ...item } : null;
        if (!state.item) {
            state.requestToken += 1;
            setLoadingState(false);
            return;
        }

        renderBase();
        fetchAndRender(state.item);
    }

    function clear() {
        state.item = null;
        state.requestToken += 1;
        setLoadingState(false);
    }

    function init(callbacks = {}) {
        if (state.initialized) return;
        state.initialized = true;
        state.callbacks = callbacks;

        try {
            state.collapsed = localStorage.getItem(STORAGE_KEY) === '1';
        } catch (_) {
            state.collapsed = false;
        }

        applyCollapsedState();

        const toggle = node('playerMetaToggle');
        if (toggle) {
            toggle.addEventListener('click', toggleCollapsed);
        }

        const prevCover = node('playerMetaCoverPrev');
        if (prevCover) {
            prevCover.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                shiftCover(-1);
            });
        }

        const nextCover = node('playerMetaCoverNext');
        if (nextCover) {
            nextCover.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                shiftCover(1);
            });
        }

        const bookmark = node('playerMetaBookmark');
        if (bookmark) {
            bookmark.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (!state.item || typeof state.callbacks.toggleBookmark !== 'function') return;
                const bookmarked = state.callbacks.toggleBookmark(state.item);
                bookmark.classList.toggle('active', !!bookmarked);
            });
        }

        const description = node('playerMetaDescription');
        if (description) {
            description.addEventListener('click', () => {
                if (description.classList.contains('empty')) return;
                description.classList.toggle('expanded');
            });
        }
    }

    window.alphyPlayerMeta = {
        init,
        setItem,
        clear,
        syncBookmark: updateBookmarkState,
    };
})();
