(() => {
    const PUBLIC_ENDPOINT = '/api/lists';
    const ADMIN_ENDPOINT = '/api/admin/lists';
    const ADMIN_CHECK = '/api/admin/check';
    const BACKUP_KEY = 'alphy_admin_lists_backup_v1';
    let lists = [];
    let isAdmin = false;
    let saveTimer = null;
    let listPicker = null;
    let pendingPickerItem = null;
    let autoRestoreAttempted = false;

    function getAdminAuth() {
        return window.getAdminAuth ? window.getAdminAuth() : null;
    }

    function adminHeaders() {
        const auth = getAdminAuth();
        if (auth?.user && auth?.pass) {
            return { 'X-Admin-User': auth.user, 'X-Admin-Pass': auth.pass };
        }
        return {};
    }

    function loadBackupLists() {
        try {
            const raw = localStorage.getItem(BACKUP_KEY);
            if (!raw) return [];
            const parsed = JSON.parse(raw);
            const backupLists = parsed?.lists;
            return Array.isArray(backupLists) ? backupLists : [];
        } catch {
            return [];
        }
    }

    function saveBackupLists(items) {
        try {
            localStorage.setItem(BACKUP_KEY, JSON.stringify({
                ts: Date.now(),
                lists: items
            }));
        } catch {
            // ignore
        }
    }

    function setAdminMode(enabled) {
        isAdmin = !!enabled;
        document.body.classList.toggle('admin-mode', isAdmin);
        const entry = document.getElementById('adminEntry');
        if (entry) {
            entry.textContent = isAdmin ? 'log out' : 'admin entry';
        }
    }

    async function restoreAdminIfPossible() {
        const auth = getAdminAuth();
        if (!auth?.user || !auth?.pass) return;
        try {
            const response = await fetch(ADMIN_CHECK, { headers: adminHeaders() });
            if (response.ok) {
                setAdminMode(true);
            }
        } catch {
            setAdminMode(false);
        }
    }

    async function ensureAdmin() {
        if (isAdmin) return true;
        if (window.requestAdminAccess) {
            const ok = await window.requestAdminAccess();
            if (ok) {
                setAdminMode(true);
                return true;
            }
        }
        return false;
    }

    function normalizeItem(item) {
        return {
            type: item.type,
            id: item.id,
            title: item.title || '',
            year: item.year || '',
            poster: item.poster || '',
            url: item.url || ''
        };
    }

    function getItemKey(item) {
        if (item.url) return item.url;
        if (item.type && item.id) return `${item.type}:${item.id}`;
        return item.title || '';
    }

    function scheduleSave() {
        if (!isAdmin) return;
        saveBackupLists(lists);
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            saveLists();
        }, 400);
    }

    async function fetchLists() {
        try {
            const response = await fetch(PUBLIC_ENDPOINT);
            if (!response.ok) return;
            const data = await response.json();
            lists = Array.isArray(data?.lists) ? data.lists : [];
            render();
            if (!lists.length) {
                await autoRestoreFromBackup();
            }
        } catch {
            // ignore
        }
    }

    async function saveLists() {
        if (!isAdmin) return;
        try {
            const response = await fetch(ADMIN_ENDPOINT, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    ...adminHeaders()
                },
                body: JSON.stringify({ lists })
            });
            if (response.status === 401) {
                setAdminMode(false);
                return;
            }
            if (response.ok) {
                const data = await response.json();
                lists = Array.isArray(data?.lists) ? data.lists : lists;
                saveBackupLists(lists);
                render();
            }
        } catch {
            // ignore
        }
    }

    async function autoRestoreFromBackup() {
        if (autoRestoreAttempted) return;
        autoRestoreAttempted = true;

        const backupLists = loadBackupLists();
        if (!backupLists.length) return;

        const auth = getAdminAuth();
        if (!auth?.user || !auth?.pass) return;

        try {
            const checkResponse = await fetch(ADMIN_CHECK, { headers: adminHeaders() });
            if (!checkResponse.ok) return;
            setAdminMode(true);

            const restoreResponse = await fetch(ADMIN_ENDPOINT, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    ...adminHeaders()
                },
                body: JSON.stringify({ lists: backupLists })
            });
            if (!restoreResponse.ok) return;

            const restored = await restoreResponse.json();
            lists = Array.isArray(restored?.lists) ? restored.lists : backupLists;
            render();
        } catch {
            // ignore
        }
    }

    function isHomeView() {
        const playerActive = document.getElementById('playerSection')?.classList.contains('active');
        const episodeActive = document.getElementById('episodeSection')?.classList.contains('active');
        const resultsActive = document.getElementById('resultsSection')?.classList.contains('active');
        const bookmarksActive = document.getElementById('bookmarksSection')?.classList.contains('active');
        return !(playerActive || episodeActive || resultsActive || bookmarksActive);
    }

    function render() {
        const section = document.getElementById('adminListsSection');
        const container = document.getElementById('listsContainer');
        if (!section || !container) return;

        const active = isHomeView() && (lists.length > 0 || isAdmin);
        section.classList.toggle('active', active);
        if (!active) {
            container.innerHTML = '';
            return;
        }

        container.innerHTML = '';
        if (!lists.length) {
            const empty = document.createElement('div');
            empty.style.color = '#6f6f6f';
            empty.style.fontSize = '13px';
            empty.textContent = 'Списков пока нет';
            container.appendChild(empty);
            return;
        }
        lists.forEach((list, listIndex) => {
            const block = document.createElement('div');
            block.className = 'list-block';
            const editableAttr = isAdmin ? 'contenteditable="true"' : '';
            const layout = computeLayout(list.items?.length || 0);
            const displayItems = buildDisplayItems(list.items || [], layout.rows, layout.mode);
            block.innerHTML = `
                <div class="list-header-row">
                    <div class="list-title" data-list-index="${listIndex}" ${editableAttr}>${escapeHtml(list.title || 'Новый список')}</div>
                    <div class="list-controls admin-only">
                        <button type="button" data-action="up" data-list-index="${listIndex}">↑</button>
                        <button type="button" data-action="down" data-list-index="${listIndex}">↓</button>
                        <button type="button" data-action="delete" data-list-index="${listIndex}">×</button>
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
            const visibleColumns = computeVisibleColumns(block);
            if (grid && visibleColumns) {
                grid.style.setProperty('--list-columns', visibleColumns);
            }
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

            if (isAdmin) {
                addListItemControls(grid, listIndex);
            }
            setupListNav(block);
        });

        attachHeaderEvents();
    }

    function attachHeaderEvents() {
        if (!isAdmin) return;
        document.querySelectorAll('.list-title[contenteditable="true"]').forEach(titleEl => {
            titleEl.onkeydown = (event) => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    titleEl.blur();
                }
            };
            titleEl.onblur = () => {
                const listIndex = Number(titleEl.dataset.listIndex);
                if (!Number.isInteger(listIndex) || !lists[listIndex]) return;
                const text = titleEl.textContent?.trim() || 'Новый список';
                lists[listIndex].title = text;
                scheduleSave();
            };
        });

        document.querySelectorAll('.list-controls button').forEach(btn => {
            btn.onclick = (event) => {
                event.preventDefault();
                const action = btn.dataset.action;
                const listIndex = Number(btn.dataset.listIndex);
                if (!Number.isInteger(listIndex) || !lists[listIndex]) return;
                if (action === 'delete') {
                    lists.splice(listIndex, 1);
                } else if (action === 'up' && listIndex > 0) {
                    const temp = lists[listIndex - 1];
                    lists[listIndex - 1] = lists[listIndex];
                    lists[listIndex] = temp;
                } else if (action === 'down' && listIndex < lists.length - 1) {
                    const temp = lists[listIndex + 1];
                    lists[listIndex + 1] = lists[listIndex];
                    lists[listIndex] = temp;
                }
                scheduleSave();
                render();
            };
        });
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
                if (idx < base.length) {
                    reordered.push(base[idx]);
                }
            }
        }
        return reordered;
    }

    function computeVisibleColumns(block) {
        const grid = block?.querySelector('.list-grid');
        if (!grid) return 7;
        const gridStyles = getComputedStyle(grid);
        const paddingLeft = parseFloat(gridStyles.paddingLeft) || 0;
        const paddingRight = parseFloat(gridStyles.paddingRight) || 0;
        const width = (grid.clientWidth - paddingLeft - paddingRight) || 0;
        const rootStyles = getComputedStyle(document.documentElement);
        const cardSize = parseFloat(rootStyles.getPropertyValue('--card-size')) || 145;
        const gap = parseFloat(rootStyles.getPropertyValue('--card-gap')) || 22;
        if (!width) return 7;
        const columns = Math.max(1, Math.floor((width + gap) / (cardSize + gap)));
        return columns;
    }

    function addListItemControls(grid, listIndex) {
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
            controls.querySelectorAll('button').forEach(btn => {
                btn.addEventListener('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    const dir = btn.dataset.dir ? Number(btn.dataset.dir) : 0;
                    if (btn.classList.contains('remove')) {
                        lists[listIndex].items.splice(itemIndex, 1);
                    } else {
                        const targetIndex = itemIndex + dir;
                        if (targetIndex < 0 || targetIndex >= lists[listIndex].items.length) return;
                        const temp = lists[listIndex].items[targetIndex];
                        lists[listIndex].items[targetIndex] = lists[listIndex].items[itemIndex];
                        lists[listIndex].items[itemIndex] = temp;
                    }
                    scheduleSave();
                    render();
                });
            });
            const media = card.querySelector('.result-media');
            if (media) {
                media.appendChild(controls);
            }
        });
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
            grid.scrollBy({ left: -Math.max(grid.clientWidth * 0.8, 200), behavior: 'smooth' });
        };
        nextBtn.onclick = () => {
            grid.scrollBy({ left: Math.max(grid.clientWidth * 0.8, 200), behavior: 'smooth' });
        };

        grid.addEventListener('scroll', updateNav, { passive: true });
        window.addEventListener('resize', updateNav);
        updateNav();
    }

    function openListPicker(anchor, item) {
        if (!listPicker) return;
        pendingPickerItem = item;
        listPicker.innerHTML = '';

        if (!lists.length) {
            const empty = document.createElement('div');
            empty.textContent = 'Списков нет';
            empty.style.padding = '6px 8px';
            listPicker.appendChild(empty);
        }

        lists.forEach((list, index) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = list.title || `Список ${index + 1}`;
            btn.addEventListener('click', () => {
                addItemToList(index, pendingPickerItem);
                closeListPicker();
            });
            listPicker.appendChild(btn);
        });

        const newBtn = document.createElement('button');
        newBtn.type = 'button';
        newBtn.className = 'list-picker-new';
        newBtn.textContent = '+ Новый список';
        newBtn.addEventListener('click', async () => {
            const created = await createList();
            if (created) {
                addItemToList(lists.length - 1, pendingPickerItem);
            }
            closeListPicker();
        });
        listPicker.appendChild(newBtn);

        listPicker.style.display = 'block';
        const rect = anchor.getBoundingClientRect();
        const pickerRect = listPicker.getBoundingClientRect();
        const top = Math.min(window.innerHeight - pickerRect.height - 12, rect.bottom + 8);
        const left = Math.min(window.innerWidth - pickerRect.width - 12, rect.left);
        listPicker.style.top = `${Math.max(12, top)}px`;
        listPicker.style.left = `${Math.max(12, left)}px`;
    }

    function closeListPicker() {
        if (!listPicker) return;
        listPicker.style.display = 'none';
        pendingPickerItem = null;
    }

    async function createList() {
        if (!isAdmin) {
            const ok = await ensureAdmin();
            if (!ok) return false;
        }
        lists.push({
            id: (crypto?.randomUUID?.() || Math.random().toString(36).slice(2)),
            title: 'Новый список',
            items: []
        });
        scheduleSave();
        render();
        return true;
    }

    function addItemToList(listIndex, item) {
        if (!item || !lists[listIndex]) return;
        const existing = lists[listIndex].items || [];
        const key = getItemKey(item);
        if (existing.some(entry => getItemKey(entry) === key)) return;
        lists[listIndex].items.push(normalizeItem(item));
        scheduleSave();
        render();
    }

    function onAdminEntryClick() {
        if (isAdmin) {
            if (window.setAdminAuth) {
                window.setAdminAuth('', '');
            }
            sessionStorage.removeItem('alphyAdminAuth');
            setAdminMode(false);
            render();
            return;
        }
        ensureAdmin().then(ok => {
            if (ok) {
                setAdminMode(true);
                render();
            }
        });
    }

    function attachGlobalEvents() {
        document.addEventListener('click', (event) => {
            const addBtn = event.target.closest('.admin-add-btn');
            if (addBtn) {
                event.preventDefault();
                event.stopPropagation();
                if (!isAdmin) {
                    ensureAdmin().then(ok => {
                        if (!ok) return;
                        const card = addBtn.closest('.result-card');
                        const item = window.decodeItem ? window.decodeItem(card?.dataset?.item) : null;
                        if (item) openListPicker(addBtn, item);
                    });
                } else {
                    const card = addBtn.closest('.result-card');
                    const item = window.decodeItem ? window.decodeItem(card?.dataset?.item) : null;
                    if (item) openListPicker(addBtn, item);
                }
                return;
            }

            if (listPicker && !listPicker.contains(event.target)) {
                closeListPicker();
            }
        }, true);
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function init() {
        listPicker = document.createElement('div');
        listPicker.className = 'list-picker';
        document.body.appendChild(listPicker);

        const adminEntry = document.getElementById('adminEntry');
        if (adminEntry) {
            adminEntry.addEventListener('click', onAdminEntryClick);
        }

        setAdminMode(false);

        const addListBtn = document.getElementById('addListBtn');
        if (addListBtn) {
            addListBtn.addEventListener('click', async () => {
                const ok = await ensureAdmin();
                if (!ok) return;
                await createList();
            });
        }

        attachGlobalEvents();
        restoreAdminIfPossible();
        fetchLists();
        window.addEventListener('resize', () => {
            render();
        });
    }

    window.alphyAdminLists = {
        init,
        render
    };
})();
