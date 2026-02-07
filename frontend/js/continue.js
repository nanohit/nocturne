(() => {
    const STORAGE_KEY = 'alphy_soap_watch';
    const MAX_ITEMS = 10;
    let config = null;
    let progressInterval = null;
    let lastMeta = null;

    function getHistory() {
        try {
            return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
        } catch {
            return [];
        }
    }

    function saveHistory(history) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(history));
    }

    function buildKey(entry) {
        return `${entry.type}:${entry.id}`;
    }

    function updateProgress(data) {
        let history = getHistory();
        const key = buildKey(data);
        const existingIndex = history.findIndex(item => item.key === key);
        const existing = existingIndex >= 0 ? history[existingIndex] : null;
        const progress = data.duration > 0 ? data.currentTime / data.duration : 0;
        const snapshot = data.type === 'movie'
            ? (data.snapshot ?? (existing ? existing.snapshot : ''))
            : '';

        const entry = {
            key,
            id: data.id,
            type: data.type,
            title: data.title,
            poster: data.poster || '',
            year: data.year || '',
            season: data.season ?? null,
            episode: data.episode ?? null,
            currentTime: data.currentTime,
            duration: data.duration,
            progress,
            timestamp: Date.now(),
            nextSeason: data.nextSeason ?? null,
            nextEpisode: data.nextEpisode ?? null,
            finished: progress >= 0.9,
            snapshot
        };
        lastMeta = { ...entry };

        if (entry.type === 'movie' && entry.finished) {
            if (existingIndex >= 0) {
                history.splice(existingIndex, 1);
            }
            saveHistory(history);
            renderContinue();
            return;
        }
        if (existingIndex >= 0) {
            history[existingIndex] = entry;
        } else {
            history.unshift(entry);
        }
        history.sort((a, b) => b.timestamp - a.timestamp);
        history = history.slice(0, MAX_ITEMS);
        saveHistory(history);
        renderContinue();
    }

    function removeEntry(entry) {
        let history = getHistory();
        history = history.filter(item => item.key !== entry.key);
        saveHistory(history);
        renderContinue();
    }

    function getNextEpisodeInfo() {
        if (!config) return { nextSeason: null, nextEpisode: null };
        const current = config.getCurrent?.();
        if (!current || current.type !== 'series') return { nextSeason: null, nextEpisode: null };
        const season = config.getSeason?.();
        const episode = config.getEpisode?.();
        const seasons = config.getSeriesSeasons?.() || [];
        const currentSeasonData = config.getSeasonData?.()[season];
        const episodes = currentSeasonData?.episodes?.map(ep => ep.episode) || [];
        const epIndex = episodes.indexOf(episode);
        if (epIndex >= 0 && epIndex < episodes.length - 1) {
            return { nextSeason: season, nextEpisode: episodes[epIndex + 1] };
        }
        const seasonIndex = seasons.indexOf(season);
        if (seasonIndex >= 0 && seasonIndex < seasons.length - 1) {
            return { nextSeason: seasons[seasonIndex + 1], nextEpisode: 1 };
        }
        return { nextSeason: null, nextEpisode: null };
    }

    function startTracking() {
        stopTracking();
        const tick = () => {
            if (!config) return;
            const player = config.getPlayer?.();
            const current = config.getCurrent?.();
            if (!player || !current) return;
            const duration = player.duration?.() || 0;
            const currentTime = player.currentTime?.() || 0;
            if (!duration || duration <= 0) return;

            const isSeries = current.type === 'series';
            const season = isSeries ? config.getSeason?.() : null;
            const episode = isSeries ? config.getEpisode?.() : null;
            const { nextSeason, nextEpisode } = isSeries ? getNextEpisodeInfo() : { nextSeason: null, nextEpisode: null };

            updateProgress({
                id: current.id,
                type: current.type,
                title: current.title,
                poster: current.poster,
                year: current.year,
                season,
                episode,
                currentTime,
                duration,
                nextSeason,
                nextEpisode
            });
        };
        tick();
        progressInterval = setInterval(tick, 5000);
    }

    function primeSnapshotMeta() {
        if (!config) return;
        const current = config.getCurrent?.();
        if (!current) return;
        lastMeta = {
            id: current.id,
            type: current.type,
            title: current.title,
            poster: current.poster,
            year: current.year,
            season: current.type === 'series' ? config.getSeason?.() : null,
            episode: current.type === 'series' ? config.getEpisode?.() : null
        };
    }

    function stopTracking() {
        if (progressInterval) {
            clearInterval(progressInterval);
            progressInterval = null;
        }
    }

    function markEnded() {
        if (!config) return;
        const player = config.getPlayer?.();
        const current = config.getCurrent?.();
        if (!player || !current) return;
        const duration = player.duration?.() || 0;
        if (!duration || duration <= 0) return;

        const isSeries = current.type === 'series';
        const season = isSeries ? config.getSeason?.() : null;
        const episode = isSeries ? config.getEpisode?.() : null;
        const { nextSeason, nextEpisode } = isSeries ? getNextEpisodeInfo() : { nextSeason: null, nextEpisode: null };

        updateProgress({
            id: current.id,
            type: current.type,
            title: current.title,
            poster: current.poster,
            year: current.year,
            season,
            episode,
            currentTime: duration,
            duration,
            nextSeason,
            nextEpisode
        });
    }

    function formatMinutesLeft(entry, withPrefix = false) {
        const remaining = Math.max(0, Math.ceil((entry.duration - entry.currentTime) / 60));
        const suffix = minutesSuffix(remaining);
        if (!withPrefix) {
            return `${remaining} ${suffix}`;
        }
        const prefix = remainingPrefix(remaining);
        return `${prefix} ${remaining} ${suffix}`;
    }

    function minutesSuffix(value) {
        const mod100 = value % 100;
        if (mod100 >= 11 && mod100 <= 19) return 'минут';
        const mod10 = value % 10;
        if (mod10 === 1) return 'минута';
        if (mod10 >= 2 && mod10 <= 4) return 'минуты';
        return 'минут';
    }

    function remainingPrefix(value) {
        const mod100 = value % 100;
        if (mod100 >= 11 && mod100 <= 19) return 'осталось';
        const mod10 = value % 10;
        if (mod10 === 1) return 'осталась';
        return 'осталось';
    }

    function renderContinue() {
        const section = document.getElementById('continueSection');
        const track = document.getElementById('continueTrack');
        const prevBtn = section?.querySelector('.continue-nav.prev');
        const nextBtn = section?.querySelector('.continue-nav.next');
        if (!section || !track) return;

        syncContinueSize();

        const history = getHistory();
        const deduped = [];
        const seen = new Set();
        history.sort((a, b) => b.timestamp - a.timestamp).forEach(item => {
            const key = `${item.type}:${item.id}`;
            if (seen.has(key)) return;
            seen.add(key);
            deduped.push(item);
        });

        const items = deduped.filter(item => {
            if (item.type === 'movie' && (item.progress || 0) >= 0.9) return false;
            if (item.type === 'series' && item.finished && item.nextEpisode !== null) return true;
            return !item.finished;
        }).slice(0, MAX_ITEMS);

        const isHome = config?.isHome ? config.isHome() : true;
        if (!items.length || !isHome) {
            section.classList.remove('active');
            track.innerHTML = '';
            return;
        }

        track.innerHTML = '';

        items.forEach((item, index) => {
            const isNext = item.type === 'series' && item.finished && item.nextEpisode !== null;
            const isWide = index === 0;
            const card = document.createElement('div');
            card.className = `continue-card ${isWide ? 'wide' : 'square'}`;

            let metaLeft = '';
            if (isNext) {
                metaLeft = `next - S${item.nextSeason}E${item.nextEpisode}`;
            } else if (item.type === 'series') {
                metaLeft = isWide ? `${formatMinutesLeft(item, true)}` : `S${item.season}E${item.episode}`;
            } else {
                metaLeft = isWide ? `${formatMinutesLeft(item, true)}` : `${formatMinutesLeft(item)}`;
            }

            const metaRight = (!isWide && item.type === 'series' && !isNext)
                ? formatMinutesLeft(item)
                : '';

            const progressWidth = isNext ? 0 : Math.round((item.progress || 0) * 100);
            const resumeTime = isNext ? 0 : item.currentTime || 0;
            const useSnapshot = isWide && !isNext && item.snapshot;
            const imageSrc = useSnapshot ? item.snapshot : (item.poster || 'https://via.placeholder.com/300x300?text=No+Image');
            const playBadge = isWide ? '<div class="continue-play"></div>' : '';

            card.innerHTML = `
                <button class="continue-remove" type="button" aria-label="Remove">&times;</button>
                <div class="continue-media">
                    <img src="${imageSrc}" alt="${item.title}">
                    <div class="continue-overlay"></div>
                    ${playBadge}
                    <div class="continue-meta ${isWide ? 'wide-meta' : ''}">
                        <span>${metaLeft}</span>
                        ${metaRight ? `<span>${metaRight}</span>` : ''}
                    </div>
                    <div class="continue-progress">
                        <div class="continue-progress-bar" style="width: ${progressWidth}%"></div>
                    </div>
                </div>
                <div class="continue-title">${item.title}</div>
                <div class="continue-sub">${item.year || ''}</div>
            `;

            card.querySelector('.continue-remove').addEventListener('click', (event) => {
                event.stopPropagation();
                removeEntry(item);
            });

            card.addEventListener('click', () => {
                if (!config) return;
                if (item.type === 'movie') {
                    config.playMovie?.(item, resumeTime);
                } else {
                    const season = isNext ? item.nextSeason : item.season;
                    const episode = isNext ? item.nextEpisode : item.episode;
                    config.playSeries?.(item, season, episode, resumeTime);
                }
            });

            track.appendChild(card);
        });

        section.classList.add('active');

        const updateNav = () => {
            if (!prevBtn || !nextBtn) return;
            const maxScroll = track.scrollWidth - track.clientWidth;
            prevBtn.classList.toggle('visible', track.scrollLeft > 4);
            nextBtn.classList.toggle('visible', maxScroll > 4 && track.scrollLeft < maxScroll - 4);
        };

        if (prevBtn && nextBtn) {
            prevBtn.onclick = () => {
                track.scrollBy({ left: -Math.max(track.clientWidth * 0.8, 200), behavior: 'smooth' });
            };
            nextBtn.onclick = () => {
                track.scrollBy({ left: Math.max(track.clientWidth * 0.8, 200), behavior: 'smooth' });
            };
            track.addEventListener('scroll', updateNav, { passive: true });
            window.addEventListener('resize', updateNav);
        }
        updateNav();
    }

    function syncContinueSize() {
        const row = document.querySelector('.continue-row');
        const container = document.querySelector('.container');
        if (!row || !container) return;
        const containerStyles = getComputedStyle(container);
        const paddingLeft = parseFloat(containerStyles.paddingLeft) || 0;
        const paddingRight = parseFloat(containerStyles.paddingRight) || 0;
        const rootStyles = getComputedStyle(document.documentElement);
        const minSize = parseFloat(rootStyles.getPropertyValue('--card-min')) || 145;
        const gap = parseFloat(rootStyles.getPropertyValue('--card-gap')) || 22;
        const width = (container.clientWidth - paddingLeft - paddingRight) || 0;
        if (!width) return;
        const columns = Math.max(1, Math.floor((width + gap) / (minSize + gap)));
        const size = (width - gap * (columns - 1)) / columns;
        row.style.setProperty('--continue-size', `${size.toFixed(2)}px`);
        document.documentElement.style.setProperty('--card-size', `${size.toFixed(2)}px`);
    }

    function init(cfg) {
        config = cfg;
        window.addEventListener('resize', syncContinueSize);
        renderContinue();
    }

    function captureSnapshot() {
        if (!config) return;
        const player = config.getPlayer?.();
        const current = lastMeta || config.getCurrent?.();
        if (!player || !current) return;
        if (current.type === 'series') return;
        const videoEl = config.getVideoElement?.();
        if (!videoEl || !videoEl.videoWidth || !videoEl.videoHeight) return;
        const duration = player.duration?.() || 0;
        const currentTime = player.currentTime?.() || 0;
        if (!duration || duration <= 0) return;

        const scale = Math.min(480 / videoEl.videoWidth, 1);
        const width = Math.round(videoEl.videoWidth * scale);
        const height = Math.round(videoEl.videoHeight * scale);

        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const isSeries = current.type === 'series';
        const season = current.season ?? (isSeries ? config.getSeason?.() : null);
        const episode = current.episode ?? (isSeries ? config.getEpisode?.() : null);
        const { nextSeason, nextEpisode } = isSeries ? getNextEpisodeInfo() : { nextSeason: null, nextEpisode: null };
        try {
            ctx.drawImage(videoEl, 0, 0, width, height);
            const snapshot = canvas.toDataURL('image/jpeg', 0.7);
            updateProgress({
                id: current.id,
                type: current.type,
                title: current.title,
                poster: current.poster,
                year: current.year,
                season,
                episode,
                currentTime,
                duration,
                nextSeason,
                nextEpisode,
                snapshot
            });
        } catch {
            // if tainted, clear snapshot to avoid stale frames
            updateProgress({
                id: current.id,
                type: current.type,
                title: current.title,
                poster: current.poster,
                year: current.year,
                season,
                episode,
                currentTime,
                duration,
                nextSeason,
                nextEpisode,
                snapshot: ''
            });
        }
    }

    window.alphyContinue = {
        init,
        render: renderContinue,
        startTracking,
        stopTracking,
        markEnded,
        captureSnapshot,
        primeSnapshotMeta
    };
})();
