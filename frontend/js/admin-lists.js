(() => {
    const PUBLIC_ENDPOINT = '/api/lists';
    const ADMIN_ENDPOINT = '/api/admin/lists';
    const ADMIN_SYNC_ENDPOINT = '/api/admin/lists/sync';
    const ADMIN_CHECK_ENDPOINT = '/api/admin/check';
    const BACKUP_KEY = 'alphy_admin_lists_backup_v2';
    const SAVE_DEBOUNCE_MS = 600;

    const state = {
        lists: [],
        revision: 0,
        updatedAt: null,
        isAdmin: false,
        saveTimer: null,
        saveInFlight: false,
        saveQueued: false,
        dirty: false,
        listPicker: null,
        pendingPickerItem: null,
        saveBadge: null,
    };

    function node(id) {
        return document.getElementById(id);
    }

    function getAdminAuth() {
        return window.getAdminAuth ? window.getAdminAuth() : null;
    }

    function setAdminAuth(user, pass) {
        if (window.setAdminAuth) {
            window.setAdminAuth(user, pass);
        }
    }

    function toBase64(value) {
        try {
            return btoa(value);
        } catch {
            return btoa(unescape(encodeURIComponent(value)));
        }
    }

    function buildAdminToken() {
        const auth = getAdminAuth();
        if (!auth?.user || !auth?.pass) return null;
        return toBase64(`${auth.user}:${auth.pass}`);
    }

    function adminHeaders() {
        const auth = getAdminAuth();
        if (auth?.user && auth?.pass) {
            return {
                'X-Admin-User': auth.user,
                'X-Admin-Pass': auth.pass,
            };
        }
        return {};
    }

    function isEditableTarget(target) {
        if (!target) return false;
        if (target.isContentEditable) return true;
        const tag = (target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
        return !!target.closest?.('[contenteditable="true"]');
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function normalizeItem(item) {
        return {
            type: String(item?.type || ''),
            id: String(item?.id || ''),
            title: String(item?.title || '').trim(),
            year: item?.year ?? '',
            poster: item?.poster || '',
            url: item?.url || '',
        };
    }

    function normalizeLists(rawLists) {
        if (!Array.isArray(rawLists)) return [];
        const out = [];
        rawLists.forEach((entry) => {
            if (!entry || typeof entry !== 'object') return;
            const listId = String(entry.id || crypto?.randomUUID?.() || Math.random().toString(36).slice(2));
            const listTitle = String(entry.title || '').trim() || 'Новый список';
            const items = [];
            const seen = new Set();
            (Array.isArray(entry.items) ? entry.items : []).forEach((item) => {
                const normalized = normalizeItem(item);
                if (!normalized.type || !normalized.id) return;
                const key = normalized.url || `${normalized.type}:${normalized.id}`;
                if (seen.has(key)) return;
                seen.add(key);
                items.push(normalized);
            });
            out.push({
                id: listId,
                title: listTitle,
                items,
            });
        });
        return out;
    }

    function normalizeEnvelope(data) {
        const lists = normalizeLists(data?.lists);
        let revision = Number(data?.revision ?? (lists.length ? 1 : 0));
        if (!Number.isFinite(revision) || revision < 0) revision = 0;
        if (revision === 0 && lists.length) revision = 1;
        const updatedAt = data?.updated_at ? String(data.updated_at) : null;
        return { lists, revision, updatedAt };
    }

    function listsFingerprint(lists) {
        return JSON.stringify(
            normalizeLists(lists).map((list) => ({
                id: list.id,
                title: list.title,
                items: list.items.map((item) => ({
                    type: item.type,
                    id: item.id,
                    title: item.title,
                    year: item.year,
                    poster: item.poster,
                    url: item.url,
                })),
            }))
        );
    }

    function sameLists(a, b) {
        return listsFingerprint(a) === listsFingerprint(b);
    }

    function loadBackupEnvelope() {
        try {
            const raw = localStorage.getItem(BACKUP_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            const normalized = normalizeEnvelope(parsed);
            const savedAt = Number(parsed?.saved_at || parsed?.savedAt || 0);
            return {
                ...normalized,
                savedAt: Number.isFinite(savedAt) ? savedAt : 0,
            };
        } catch {
            return null;
        }
    }

    function saveBackupEnvelope(savedAt = Date.now()) {
        try {
            localStorage.setItem(BACKUP_KEY, JSON.stringify({
                lists: state.lists,
                revision: state.revision,
                updated_at: state.updatedAt,
                saved_at: savedAt,
            }));
        } catch {
            // ignore localStorage errors
        }
    }

    function setSaveBadge(mode, text) {
        if (!state.saveBadge) state.saveBadge = node('adminSaveBadge');
        if (!state.saveBadge) return;
        state.saveBadge.classList.remove('state-dirty', 'state-saving', 'state-ok', 'state-error');
        if (mode) state.saveBadge.classList.add(`state-${mode}`);
        state.saveBadge.textContent = text;
    }

    function setAdminMode(enabled) {
        state.isAdmin = !!enabled;
        document.body.classList.toggle('admin-mode', state.isAdmin);
        const entry = node('adminEntry');
        if (entry) entry.textContent = state.isAdmin ? 'log out' : 'admin entry';

        if (!state.isAdmin) {
            setSaveBadge('ok', 'read-only');
        } else if (state.dirty) {
            setSaveBadge('dirty', 'unsaved');
        } else if (state.updatedAt) {
            setSaveBadge('ok', 'saved');
        } else {
            setSaveBadge('ok', 'ready');
        }
    }

    async function restoreAdminIfPossible() {
        const auth = getAdminAuth();
        if (!auth?.user || !auth?.pass) {
            setAdminMode(false);
            return false;
        }
        try {
            const response = await fetch(ADMIN_CHECK_ENDPOINT, { headers: adminHeaders() });
            if (response.ok) {
                setAdminMode(true);
                return true;
            }
        } catch {
            // ignore
        }
        setAdminMode(false);
        return false;
    }

    async function ensureAdmin() {
        if (state.isAdmin) return true;
        if (!window.requestAdminAccess) return false;
        const ok = await window.requestAdminAccess();
        if (!ok) return false;
        setAdminMode(true);
        return true;
    }

    async function fetchServerEnvelope() {
        const response = await fetch(PUBLIC_ENDPOINT);
        if (!response.ok) {
            throw new Error(`Failed to load admin lists: ${response.status}`);
        }
        const payload = await response.json();
        return normalizeEnvelope(payload);
    }

    function shouldRestoreBackup(serverEnvelope, backupEnvelope) {
        if (!backupEnvelope || !backupEnvelope.lists.length) return false;
        if (sameLists(backupEnvelope.lists, serverEnvelope.lists)) return false;

        const serverTs = serverEnvelope.updatedAt ? Date.parse(serverEnvelope.updatedAt) : 0;
        const backupTs = Number(backupEnvelope.savedAt || 0);
        if (backupTs > serverTs) return true;
        if (!serverEnvelope.lists.length && backupEnvelope.lists.length) return true;
        return false;
    }

    async function hydrateLists() {
        try {
            const serverEnvelope = await fetchServerEnvelope();
            state.lists = serverEnvelope.lists;
            state.revision = serverEnvelope.revision;
            state.updatedAt = serverEnvelope.updatedAt;
            state.dirty = false;
            render();

            if (state.isAdmin) {
                const backup = loadBackupEnvelope();
                if (shouldRestoreBackup(serverEnvelope, backup)) {
                    state.lists = backup.lists;
                    state.revision = serverEnvelope.revision;
                    state.updatedAt = serverEnvelope.updatedAt;
                    state.dirty = true;
                    render();
                    setSaveBadge('saving', 'restoring');
                    await flushSave();
                    return;
                }
            }

            saveBackupEnvelope();
            setSaveBadge('ok', state.isAdmin ? 'saved' : 'read-only');
        } catch {
            const backup = loadBackupEnvelope();
            if (backup?.lists?.length) {
                state.lists = backup.lists;
                state.revision = backup.revision || 0;
                state.updatedAt = backup.updatedAt || null;
                state.dirty = false;
                render();
                setSaveBadge('error', state.isAdmin ? 'offline backup' : 'backup');
                return;
            }
            setSaveBadge('error', 'load failed');
        }
    }

    function isHomeView() {
        const playerActive = node('playerSection')?.classList.contains('active');
        const episodeActive = node('episodeSection')?.classList.contains('active');
        const resultsActive = node('resultsSection')?.classList.contains('active');
        const bookmarksActive = node('bookmarksSection')?.classList.contains('active');
        return !(playerActive || episodeActive || resultsActive || bookmarksActive);
    }

    function markDirty() {
        if (!state.isAdmin) return;
        state.dirty = true;
        saveBackupEnvelope();
        setSaveBadge('dirty', 'unsaved');
        scheduleSave(false);
    }

    function scheduleSave(immediate) {
        if (!state.isAdmin) return;
        if (immediate) {
            if (state.saveTimer) {
                clearTimeout(state.saveTimer);
                state.saveTimer = null;
            }
            flushSave();
            return;
        }
        if (state.saveTimer) clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(() => {
            state.saveTimer = null;
            flushSave();
        }, SAVE_DEBOUNCE_MS);
    }

    async function doSaveRequest(baseRevision) {
        return fetch(ADMIN_ENDPOINT, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                ...adminHeaders(),
            },
            body: JSON.stringify({
                lists: state.lists,
                base_revision: baseRevision,
            }),
        });
    }

    async function flushSave() {
        if (!state.isAdmin || !state.dirty) return;
        if (state.saveInFlight) {
            state.saveQueued = true;
            return;
        }
        state.saveInFlight = true;
        setSaveBadge('saving', 'saving');

        try {
            let response = await doSaveRequest(state.revision);

            if (response.status === 401) {
                setAdminMode(false);
                setSaveBadge('error', 'auth required');
                return;
            }

            if (response.status === 409) {
                const conflict = await response.json().catch(() => ({}));
                const current = normalizeEnvelope(conflict?.detail?.current || conflict?.current || {});
                state.revision = current.revision;
                response = await doSaveRequest(state.revision);
            }

            if (!response.ok) {
                setSaveBadge('error', `save failed (${response.status})`);
                return;
            }

            const saved = normalizeEnvelope(await response.json());
            state.lists = saved.lists;
            state.revision = saved.revision;
            state.updatedAt = saved.updatedAt;
            state.dirty = false;
            saveBackupEnvelope();
            render();
            setSaveBadge('ok', 'saved');
        } catch {
            setSaveBadge('error', 'network error');
        } finally {
            state.saveInFlight = false;
            if (state.saveQueued) {
                state.saveQueued = false;
                flushSave();
            }
        }
    }

    function getItemKey(item) {
        if (!item) return '';
        if (item.url) return item.url;
        if (item.type && item.id) return `${item.type}:${item.id}`;
        return item.title || '';
    }

    function addItemToList(listIndex, item) {
        const list = state.lists[listIndex];
        if (!list || !item) return;
        const normalized = normalizeItem(item);
        if (!normalized.type || !normalized.id) return;
        const key = getItemKey(normalized);
        if (list.items.some((entry) => getItemKey(entry) === key)) return;
        list.items.push(normalized);
        markDirty();
        render();
    }

    function computeLayout(count) {
        if (count <= 7) {
            return { rows: 1, mode: 'scroll' };
        }
        const fullRows = Math.floor(count / 7);
        const remainder = count % 7;
        if (count <= 21) {
            if (remainder === 0) {
                return { rows: fullRows, mode: 'wrap' };
            }
            if (remainder >= 5 && fullRows < 3) {
                return { rows: fullRows + 1, mode: 'wrap' };
            }
            return { rows: fullRows, mode: 'scroll' };
        }
        return { rows: 3, mode: 'scroll' };
    }

    function buildDisplayItems(items, rows, mode) {
        const base = items.map((item, index) => ({ ...item, __index: index }));
        if (mode !== 'scroll' || rows <= 1) return base;
        const columns = Math.ceil(base.length / rows);
        const reordered = [];
        for (let col = 0; col < columns; col += 1) {
            for (let row = 0; row < rows; row += 1) {
                const idx = row * columns + col;
                if (idx < base.length) reordered.push(base[idx]);
            }
        }
        return reordered;
    }

    function computeVisibleColumns(grid) {
        if (!grid) return 7;
        const width = grid.clientWidth || 0;
        const rootStyles = getComputedStyle(document.documentElement);
        const cardSize = parseFloat(rootStyles.getPropertyValue('--card-size')) || 145;
        const gap = parseFloat(rootStyles.getPropertyValue('--card-gap')) || 22;
        if (!width) return 7;
        return Math.max(1, Math.floor((width + gap) / (cardSize + gap)));
    }

    function setupListNav(block) {
        const grid = block.querySelector('.list-grid');
        const prevBtn = block.querySelector('.list-nav.prev');
        const nextBtn = block.querySelector('.list-nav.next');
        if (!grid || !prevBtn || !nextBtn) return;

        const updateNav = () => {
            const maxScroll = grid.scrollWidth - grid.clientWidth;
            prevBtn.classList.toggle('visible', grid.scrollLeft > 4);
            nextBtn.classList.toggle('visible', maxScroll > 4 && grid.scrollLeft < maxScroll - 4);
        };

        prevBtn.onclick = () => {
            grid.scrollBy({ left: -Math.max(grid.clientWidth * 0.82, 220), behavior: 'smooth' });
        };
        nextBtn.onclick = () => {
            grid.scrollBy({ left: Math.max(grid.clientWidth * 0.82, 220), behavior: 'smooth' });
        };

        grid.addEventListener('scroll', updateNav, { passive: true });
        block._updateNav = updateNav;
        updateNav();
    }

    function updateAllListNavs() {
        document.querySelectorAll('.list-block').forEach((block) => {
            if (typeof block._updateNav === 'function') {
                block._updateNav();
            }
        });
    }

    function addListItemControls(grid, listIndex) {
        if (!state.isAdmin) return;
        const cards = grid.querySelectorAll('.result-card');
        cards.forEach((card) => {
            const itemIndex = Number(card.dataset.listIndex);
            if (!Number.isInteger(itemIndex)) return;
            const controls = document.createElement('div');
            controls.className = 'list-item-controls admin-only';
            controls.innerHTML = `
                <button type="button" data-dir="-1" aria-label="Move left">‹</button>
                <button type="button" data-dir="1" aria-label="Move right">›</button>
                <button type="button" class="remove" aria-label="Remove">×</button>
            `;
            controls.querySelectorAll('button').forEach((btn) => {
                btn.addEventListener('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    const list = state.lists[listIndex];
                    if (!list) return;
                    const currentIndex = Number(card.dataset.listIndex);
                    if (!Number.isInteger(currentIndex)) return;

                    if (btn.classList.contains('remove')) {
                        list.items.splice(currentIndex, 1);
                    } else {
                        const dir = Number(btn.dataset.dir || 0);
                        const target = currentIndex + dir;
                        if (target < 0 || target >= list.items.length) return;
                        const tmp = list.items[target];
                        list.items[target] = list.items[currentIndex];
                        list.items[currentIndex] = tmp;
                    }
                    markDirty();
                    render();
                });
            });
            const media = card.querySelector('.result-media');
            if (media) media.appendChild(controls);
        });
    }

    function attachHeaderEvents() {
        if (!state.isAdmin) return;

        document.querySelectorAll('.list-title[contenteditable="true"]').forEach((titleEl) => {
            titleEl.onkeydown = (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    titleEl.blur();
                }
            };
            titleEl.onblur = () => {
                const index = Number(titleEl.dataset.listIndex);
                if (!Number.isInteger(index) || !state.lists[index]) return;
                const text = titleEl.textContent?.trim() || 'Новый список';
                if (state.lists[index].title === text) return;
                state.lists[index].title = text;
                markDirty();
            };
        });

        document.querySelectorAll('.list-controls button').forEach((btn) => {
            btn.onclick = (event) => {
                event.preventDefault();
                const action = btn.dataset.action;
                const index = Number(btn.dataset.listIndex);
                if (!Number.isInteger(index) || !state.lists[index]) return;

                if (action === 'delete') {
                    state.lists.splice(index, 1);
                } else if (action === 'up' && index > 0) {
                    const tmp = state.lists[index - 1];
                    state.lists[index - 1] = state.lists[index];
                    state.lists[index] = tmp;
                } else if (action === 'down' && index < state.lists.length - 1) {
                    const tmp = state.lists[index + 1];
                    state.lists[index + 1] = state.lists[index];
                    state.lists[index] = tmp;
                }
                markDirty();
                render();
            };
        });
    }

    function render() {
        const section = node('adminListsSection');
        const container = node('listsContainer');
        if (!section || !container) return;

        const active = isHomeView() && (state.lists.length > 0 || state.isAdmin);
        section.classList.toggle('active', active);
        if (!active) {
            container.innerHTML = '';
            return;
        }

        container.innerHTML = '';
        if (!state.lists.length) {
            const empty = document.createElement('div');
            empty.className = 'list-picker-empty';
            empty.textContent = state.isAdmin ? 'No lists yet. Create one.' : 'No community lists yet.';
            container.appendChild(empty);
            return;
        }

        state.lists.forEach((list, listIndex) => {
            const block = document.createElement('div');
            block.className = 'list-block';
            const editableAttr = state.isAdmin ? 'contenteditable="true"' : '';
            const layout = computeLayout(list.items?.length || 0);
            const displayItems = buildDisplayItems(list.items || [], layout.rows, layout.mode);

            block.innerHTML = `
                <div class="list-header-row">
                    <div class="list-title" data-list-index="${listIndex}" ${editableAttr}>${escapeHtml(list.title || 'Новый список')}</div>
                    <div class="list-controls admin-only">
                        <button type="button" data-action="up" data-list-index="${listIndex}" aria-label="Move list up">↑</button>
                        <button type="button" data-action="down" data-list-index="${listIndex}" aria-label="Move list down">↓</button>
                        <button type="button" data-action="delete" data-list-index="${listIndex}" aria-label="Delete list">×</button>
                    </div>
                </div>
                <div class="list-row" data-list-index="${listIndex}">
                    <button class="list-nav prev" type="button" aria-label="Scroll left">&#8249;</button>
                    <div class="list-grid ${layout.mode === 'wrap' ? 'mode-wrap' : 'mode-scroll'}" data-list-index="${listIndex}" style="--list-rows:${layout.rows};"></div>
                    <button class="list-nav next" type="button" aria-label="Scroll right">&#8250;</button>
                </div>
            `;

            const grid = block.querySelector('.list-grid');
            container.appendChild(block);

            if (grid) {
                grid.style.setProperty('--list-columns', computeVisibleColumns(grid));
                if (window.renderCardGrid) {
                    window.renderCardGrid(grid, displayItems);
                } else {
                    grid.innerHTML = '';
                }

                const cards = grid.querySelectorAll('.result-card');
                cards.forEach((card, index) => {
                    const originalIndex = displayItems[index]?.__index ?? index;
                    card.dataset.listIndex = String(originalIndex);
                });

                addListItemControls(grid, listIndex);
            }

            setupListNav(block);
        });

        attachHeaderEvents();
        updateAllListNavs();
    }

    function openListPicker(anchor, item) {
        if (!state.listPicker) return;
        state.pendingPickerItem = item;
        state.listPicker.innerHTML = '';

        if (!state.lists.length) {
            const empty = document.createElement('div');
            empty.className = 'list-picker-empty';
            empty.textContent = 'No lists yet';
            state.listPicker.appendChild(empty);
        }

        state.lists.forEach((list, index) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = list.title || `List ${index + 1}`;
            btn.addEventListener('click', () => {
                addItemToList(index, state.pendingPickerItem);
                closeListPicker();
            });
            state.listPicker.appendChild(btn);
        });

        const newBtn = document.createElement('button');
        newBtn.type = 'button';
        newBtn.className = 'list-picker-new';
        newBtn.textContent = '+ New list';
        newBtn.addEventListener('click', async () => {
            const ok = await createList();
            if (ok) addItemToList(state.lists.length - 1, state.pendingPickerItem);
            closeListPicker();
        });
        state.listPicker.appendChild(newBtn);

        state.listPicker.style.display = 'block';
        const rect = anchor.getBoundingClientRect();
        const pickerRect = state.listPicker.getBoundingClientRect();
        const top = Math.min(window.innerHeight - pickerRect.height - 12, rect.bottom + 8);
        const left = Math.min(window.innerWidth - pickerRect.width - 12, rect.left);
        state.listPicker.style.top = `${Math.max(10, top)}px`;
        state.listPicker.style.left = `${Math.max(10, left)}px`;
    }

    function closeListPicker() {
        if (!state.listPicker) return;
        state.listPicker.style.display = 'none';
        state.pendingPickerItem = null;
    }

    async function createList() {
        if (!state.isAdmin) {
            const ok = await ensureAdmin();
            if (!ok) return false;
        }
        state.lists.push({
            id: crypto?.randomUUID?.() || Math.random().toString(36).slice(2),
            title: 'Новый список',
            items: [],
        });
        markDirty();
        render();
        return true;
    }

    async function onAdminEntryClick() {
        if (state.isAdmin) {
            setAdminAuth('', '');
            sessionStorage.removeItem('alphyAdminAuth');
            setAdminMode(false);
            closeListPicker();
            render();
            return;
        }

        const ok = await ensureAdmin();
        if (!ok) return;
        setAdminMode(true);
        await hydrateLists();
        render();
    }

    function attachGlobalEvents() {
        document.addEventListener('click', (event) => {
            if (isEditableTarget(event.target)) return;

            const addBtn = event.target.closest('.admin-add-btn');
            if (addBtn) {
                event.preventDefault();
                event.stopPropagation();
                const handleOpen = () => {
                    const card = addBtn.closest('.result-card');
                    const item = window.decodeItem ? window.decodeItem(card?.dataset?.item) : null;
                    if (item) openListPicker(addBtn, item);
                };
                if (!state.isAdmin) {
                    ensureAdmin().then((ok) => {
                        if (ok) handleOpen();
                    });
                } else {
                    handleOpen();
                }
                return;
            }

            if (state.listPicker && !state.listPicker.contains(event.target)) {
                closeListPicker();
            }
        }, true);

        window.addEventListener('resize', () => {
            render();
            updateAllListNavs();
        });

        window.addEventListener('beforeunload', () => {
            if (!state.isAdmin || !state.dirty) return;
            const auth = getAdminAuth();
            const payload = JSON.stringify({ lists: state.lists, base_revision: state.revision });
            const token = buildAdminToken();

            if (token && navigator.sendBeacon) {
                navigator.sendBeacon(`${ADMIN_SYNC_ENDPOINT}?admin_token=${encodeURIComponent(token)}`, payload);
                return;
            }

            if (auth?.user && auth?.pass) {
                fetch(ADMIN_ENDPOINT, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Admin-User': auth.user,
                        'X-Admin-Pass': auth.pass,
                    },
                    body: payload,
                    keepalive: true,
                }).catch(() => {});
            }
        });
    }

    async function init() {
        state.listPicker = document.createElement('div');
        state.listPicker.className = 'list-picker';
        document.body.appendChild(state.listPicker);
        state.saveBadge = node('adminSaveBadge');

        const adminEntry = node('adminEntry');
        if (adminEntry) adminEntry.addEventListener('click', onAdminEntryClick);

        const addListBtn = node('addListBtn');
        if (addListBtn) {
            addListBtn.addEventListener('click', async () => {
                const ok = await ensureAdmin();
                if (!ok) return;
                await createList();
            });
        }

        const syncBtn = node('syncListsBtn');
        if (syncBtn) {
            syncBtn.addEventListener('click', async () => {
                const ok = await ensureAdmin();
                if (!ok) return;
                if (state.dirty) {
                    scheduleSave(true);
                } else {
                    await hydrateLists();
                }
            });
        }

        attachGlobalEvents();
        await restoreAdminIfPossible();
        await hydrateLists();
    }

    window.alphyAdminLists = {
        init,
        render,
    };
})();
