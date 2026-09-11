// SPEC-T73b §2.3: 統合版タブシェル (/race/ 配下) に埋め込まれているかどうか。
// 本番Web (`/` 直下、Vercel) では常に false — DOM・挙動は変えない。
const IS_EMBEDDED = window.location.pathname.startsWith('/race/');

let globalHorsesData = [];
let raceCache = {}; // ◎がいるレースのハッシュマップ
let apiCache = {}; // { URL: { mode: "詳細", data: {...} } }
let pastDataCache = {}; // { apiUrl: APIレスポンス } 過去データ分析キャッシュ
let currentRaceContext = { venue: "", track_type: "", distance: 0, condition: "", race_class: "" };
let trackBiasCache = {}; // { 競馬場名: APIレスポンス }
let globalMatrixData = null; // マトリクス表示用データ
let allHorseMarks = {}; // { raceUrl: { horseNum: mark } } レースURL別の印
let currentMarkUrl = ''; // 現在表示中レースのURL
// アクティブなレースの印オブジェクトを返す（なければ初期化）
function getMarks() {
    if (!allHorseMarks[currentMarkUrl]) allHorseMarks[currentMarkUrl] = {};
    return allHorseMarks[currentMarkUrl];
}

const MARK_DEFS = [
    { mark: '◎', color: '#ef4444', label: '本命' },
    { mark: '〇', color: '#3b82f6', label: '対抗' },
    { mark: '△', color: '#22c55e', label: '注意' },
    { mark: '☆', color: '#f59e0b', label: '穴馬' },
];

function setHorseMark(num, mark) {
    const marks = getMarks();
    if (marks[num] === mark) {
        delete marks[num]; // 同じ印をクリック → 解除
    } else {
        marks[num] = mark;
    }
    document.querySelectorAll(`.mark-cell[data-num="${num}"]`).forEach(cell => {
        refreshMarkCell(cell, num);
    });
    const row = document.querySelector(`tr[data-horse-num="${num}"]`);
    if (row) updateRowMarkStyle(row, num);
}

function refreshMarkCell(cell, num) {
    const current = getMarks()[num] || '';
    cell.innerHTML = MARK_DEFS.map(({ mark, color }) => {
        const sel = current === mark;
        return `<button class="mark-btn${sel ? ' mark-sel' : ''}"
            data-mark="${mark}"
            style="${sel ? `color:${color};border-color:${color}` : ''}"
            title="${MARK_DEFS.find(d=>d.mark===mark).label}"
            onclick="setHorseMark('${num}','${mark}')">${mark}</button>`;
    }).join('');
}

function updateRowMarkStyle(row, num) {
    const mark = getMarks()[num] || '';
    const def = MARK_DEFS.find(d => d.mark === mark);
    row.style.boxShadow = def ? `inset 3px 0 0 ${def.color}` : '';
}

document.addEventListener('DOMContentLoaded', () => {
    // Tab Switching
    const tabs = document.querySelectorAll('.tab-btn');
    const contents = document.querySelectorAll('.tab-content');
    
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            contents.forEach(c => c.classList.remove('active'));
            
            tab.classList.add('active');
            document.getElementById(tab.dataset.target).classList.add('active');
        });
    });

    // Main Actions
    document.getElementById('searchBtn').addEventListener('click', startScraping);
    async function autoFetchUrl(isManual) {
        const btn = document.getElementById('getUrlBtn');
        const prevText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "取得中...";
        try {
            const day = new Date().getDay();
            const response = await fetch(`api/latest_url?day=${day}`);
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            if (data.url) {
                document.getElementById('urlInput').value = data.url;
                if (isManual) alert("最新のURLを取得しました。\n" + data.url);
            } else {
                document.getElementById('urlInput').value = "";
                if (isManual) alert("出馬表がありません。");
            }
        } catch(err) {
            if (isManual) {
                document.getElementById('urlInput').value = "";
                alert("URL取得エラー: " + err.message + "\n現在出馬表データはありません。");
            }
        } finally {
            btn.disabled = false;
            btn.textContent = "最新URL取得";
        }
    }

    document.getElementById('getUrlBtn').addEventListener('click', () => autoFetchUrl(true));

    // SPEC-T73b §2.3-2: 埋め込みモード (統合版タブシェル /race/ 配下) では
    // URL入力行 (最初の.input-group) を隠し、起動時の最新URL自動取得も呼ばない。
    if (IS_EMBEDDED) {
        const firstInputGroup = document.querySelector('.controls .input-group');
        if (firstInputGroup) firstInputGroup.style.display = 'none';
    }

    // SPEC-T73 §2.3-2 / T73b §2.3-2: ?url=...&auto=1 で開かれた場合 (オッズ監視
    // からの遷移)、urlInput にそのURLを設定し解析を自動実行する。埋め込みモード
    // では auto=1 の有無に関わらず常に自動実行する (モード選択UIが無いため)。
    let queryRaceUrl = null;
    let queryAutopick = false;
    try {
        const qs = new URLSearchParams(window.location.search);
        queryRaceUrl = qs.get('url');
        queryAutopick = qs.get('autopick') === '1';
        if (queryRaceUrl) {
            document.getElementById('urlInput').value = queryRaceUrl;
            if (IS_EMBEDDED || qs.get('auto') === '1') {
                startScraping();
            }
        }
    } catch (e) { /* noop */ }

    if (IS_EMBEDDED) {
        // SPEC-T73b §2.3-2: ?url= が無ければオッズ監視の解析状況からレース
        // 一覧を描画する (最新URL自動取得の代わり)。
        // SPEC-T77: ただし ?autopick=1 (統合版「解析開始」完了時の自動読み込み)
        // では一覧の代わりに次発走レースを自動選択する。
        if (!queryRaceUrl) {
            if (queryAutopick) {
                autoPickRaceFromEvState();
            } else {
                renderEmbeddedRaceList();
            }
        }
    } else if (!queryRaceUrl) {
        // Auto-fetch on page load (?urlで既に指定されている場合は上書きしない)
        autoFetchUrl(false);
    }

    // SPEC-T77 §2.2-4: 統合版タブシェルからの解析完了通知。レース詳細が既に
    // ロード済み (iframe.src が空でない) の場合、タブシェル側は一覧状態のまま
    // このメッセージを送ってくる。一覧状態 (queryRaceUrl 無し) の時だけ、次に
    // 発走するレースを自動選択する (レース表示中なら上書きしない)。
    if (IS_EMBEDDED) {
        window.addEventListener('message', (event) => {
            if (event.origin !== window.location.origin) return;
            const data = event.data;
            if (!data || data.type !== 'jra-analysis-done') return;
            if (!queryRaceUrl) {
                autoPickRaceFromEvState();
            }
        });
    }

    document.getElementById('historyHorseSelect').addEventListener('change', updateHistoryTable);
    document.getElementById('runAiBtn').addEventListener('click', runAiPrediction);
    
    // Past Data Analysis
    const pastDataBtn = document.getElementById('runPastDataBtn');
    if(pastDataBtn) pastDataBtn.addEventListener('click', fetchPastData);
    
    const trackBiasBtn = document.getElementById('trackBiasBtn');
    if(trackBiasBtn) trackBiasBtn.addEventListener('click', () => fetchTrackBias());

    // Auto-fetch on checkbox changes
    const classCheck = document.getElementById('matchClassCheckbox');
    const condCheck = document.getElementById('matchConditionCheckbox');
    const onToggle = () => {
        const pmContainer = document.getElementById('pastDataResultsContainer');
        if (pmContainer && pmContainer.style.display !== 'none') {
            fetchPastData();
        }
    };
    if(classCheck) classCheck.addEventListener('change', onToggle);
    if(condCheck) condCheck.addEventListener('change', onToggle);
});

// SPEC-T73b §2.3-2: 埋め込みモードで ?url= が無いとき、オッズ監視の解析状況
// (../ev/api/state) から当日のレース一覧を #raceInfo の直下に描画する。
async function renderEmbeddedRaceList() {
    const raceInfoEl = document.getElementById('raceInfo');
    raceInfoEl.textContent = 'オッズ監視でレースを選択してください';
    const old = document.getElementById('embeddedRaceList');
    if (old) old.remove();

    let races = [];
    try {
        const response = await fetch('../ev/api/state');
        const state = await response.json();
        races = (state && state.races) || [];
    } catch (e) {
        console.error(e);
    }

    const container = document.createElement('div');
    container.id = 'embeddedRaceList';
    container.className = 'embedded-race-list';

    const withUrl = races.filter(r => r && r.url);
    if (races.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'race-label';
        empty.textContent = 'オッズ監視で「解析開始」を押すと全レースが解析され、ここに一覧が出ます';
        container.appendChild(empty);
    } else {
        const byVenue = {};
        withUrl.forEach(r => {
            const venue = r.venue || '';
            (byVenue[venue] = byVenue[venue] || []).push(r);
        });
        Object.keys(byVenue).sort().forEach(venue => {
            const list = byVenue[venue].slice().sort((a, b) =>
                (a.start_time || '99:99').localeCompare(b.start_time || '99:99'));
            const block = document.createElement('div');
            block.className = 'venue-block';
            const heading = document.createElement('div');
            heading.textContent = venue;
            heading.style.fontWeight = 'bold';
            heading.style.marginBottom = '4px';
            block.appendChild(heading);
            container.appendChild(block);
            list.forEach(r => {
                const link = document.createElement('a');
                link.href = '?url=' + encodeURIComponent(r.url) + '&auto=1';
                link.textContent = r.race_info || `${r.race_num || ''}R ${r.start_time || ''}`;
                link.style.display = 'block';
                link.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    location.href = '?url=' + encodeURIComponent(r.url) + '&auto=1';
                });
                block.appendChild(link);
            });
        });
    }
    // #raceInfo は .status-panel (flex 行) の項目なので、行の外 (直後) に置く。
    // 行内に入れるとラベルが一覧の高さまで伸びて空箱に見える (2026-09-11 指摘)。
    const panel = raceInfoEl.closest('.status-panel') || raceInfoEl;
    panel.insertAdjacentElement('afterend', container);
}

// SPEC-T77 §2.2-2: 統合版「解析開始」完了時、埋め込みモードのレース詳細タブが
// 一覧状態であれば、オッズ監視の解析状況 (../ev/api/state) から「次に発走する
// レース」を選んで自動的にそのレースへ遷移する (T73b の一覧リンクと同じ遷移)。
// 選べる対象が無ければ従来どおりレース一覧を描画する。
async function autoPickRaceFromEvState() {
    const raceInfoEl = document.getElementById('raceInfo');
    if (raceInfoEl) raceInfoEl.textContent = '次のレースを自動選択中...';

    let races = [];
    try {
        const response = await fetch('../ev/api/state');
        const state = await response.json();
        races = (state && state.races) || [];
    } catch (e) {
        console.error(e);
    }

    const now = new Date();
    const nowHHMM = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
    const picked = pickNextRace(races, nowHHMM);
    if (picked) {
        location.href = '?url=' + encodeURIComponent(picked.url) + '&auto=1';
    } else {
        renderEmbeddedRaceList();
    }
}

async function startScraping() {
    const url = document.getElementById('urlInput').value.trim();
    const mode = IS_EMBEDDED ? '簡易' : document.getElementById('modeSelect').value;

    if(!url) {
        alert("URLを入力してください");
        return;
    }

    const cached = apiCache[url];
    if (cached && (mode === '簡易' || cached.mode === '詳細')) {
        applyScrapeData(cached.data, url, cached.mode);
        return;
    }

    showLoading("JRAデータを解析中... (APIリクエスト中)");
    try {
        const response = await fetch('api/scrape', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, mode })
        });
        
        const data = await response.json();
        if(data.error) throw new Error(data.error);
        
        apiCache[url] = { mode: mode, data: data };
        applyScrapeData(data, url, mode);
        
    } catch(err) {
        console.error(err);
        alert("エラーが発生しました: " + err.message);
    } finally {
        hideLoading();
    }
}

function applyScrapeData(data, url, mode) {
    const raceInfoEl = document.getElementById('raceInfo');
    raceInfoEl.textContent = `${data.race_info} (${mode})` +
        (data.analysis_message ? ` — ${data.analysis_message}` : '') +
        (data.logging_warning ? ` ⚠ ログ保存失敗: ${data.logging_warning}` : '');
    // SPEC-T73b §2.3-4: オッズ監視キャッシュから返った場合、取得時刻・ステージを
    // 小さく併記する (stage が無ければ初回解析)。
    if (data.cached_from_monitor) {
        const stageLabel = (data.monitor_stage === null || data.monitor_stage === undefined)
            ? '初回解析' : `${data.monitor_stage}分前ステージ`;
        const small = document.createElement('small');
        small.style.marginLeft = '6px';
        small.style.opacity = '0.75';
        small.textContent = `(オッズ監視 ${data.cached_from_monitor} 取得・${stageLabel})`;
        raceInfoEl.appendChild(small);
    }

    let babaHtml = data.baba_info || "馬場情報：未取得";
    if (data.course_record) {
        babaHtml += `<br><span style="color:#e0e0e0; font-size:12px; font-weight:normal;">${data.course_record}</span>`;
    }
    document.getElementById('babaInfo').innerHTML = babaHtml;
    
    const criteriaTitle = document.getElementById('criteriaTitle');
    if (criteriaTitle) {
        criteriaTitle.textContent = `【${data.venue}${data.race_type}${data.dist_val}mの好走条件】`;
    }
    
    const criteriaList = document.getElementById('criteriaList');
    criteriaList.innerHTML = data.criteria_lines.length > 0 
        ? data.criteria_lines.join('<br>') 
        : "該当なし";

    globalHorsesData = data.horses || [];
    currentMarkUrl = url; // アクティブURLを切り替え（印はURLごとに保持）
    renderHorsesTable(globalHorsesData);

    raceCache[url] = data.has_double_circle;
    globalMatrixData = data.matrix_data || null; // WIN5照合用に保存
    renderMatrix(data.matrix_data, data.venue);

    // 競馬場が変わったときにトラックバイアスを自動切替
    if (data.venue) fetchTrackBias(data.venue);
    
    let ultraText = "";
    globalHorsesData.forEach(h => {
        if(h.ultra_details && h.ultra_details.length > 0) {
            ultraText += `【${h.num}番 ${h.name}】\n`;
            h.ultra_details.forEach(d => { ultraText += `    ∟ ${d}\n` });
            ultraText += `\n`;
        }
    });
    document.getElementById('ultraDetails').textContent = ultraText || "好走条件判定に該当する馬はいません。";
    
    document.getElementById('harabValue').textContent = data.harab_index || "-";

    const courseFeatureElem = document.getElementById('courseFeatureText');
    if (courseFeatureElem) {
        const title = `<h4 style="margin-top:0; margin-bottom: 8px; font-size: 14px; border-bottom: 1px solid var(--border-color); padding-bottom: 6px;">${data.venue} ${data.race_type}${data.dist_val}m コース解説</h4>`;
        const bodyContent = data.feature_text || "このコースの過去傾向・特徴データがありません。";
        courseFeatureElem.innerHTML = title + `<div style="white-space: pre-wrap; line-height: 1.6;">${bodyContent}</div>`;
    }

    const courseImage = document.getElementById('courseLayoutImage');
    if (courseImage) {
        // SPEC-T76 §1.3: 解析のたびに前回のオーバーレイ状態をリセットする
        courseImage.dataset.loadFailed = '0';
        const windSvg = document.getElementById('windOverlay');
        const courseMapWrap = courseImage.closest('.course-map-wrap');
        const windLegendEl = document.getElementById('windLegend');
        if (windSvg) {
            windSvg.innerHTML = '';
            windSvg.setAttribute('viewBox', '0 0 570 400');
        }
        if (courseMapWrap) courseMapWrap.classList.remove('fallback');
        if (windLegendEl) windLegendEl.textContent = '';

        if (data.course_image) {
            courseImage.src = data.course_image;
        } else {
            const vEnMap = {"札幌":"sapporo", "函館":"hakodate", "福島":"fukushima", "新潟":"niigata", "東京":"tokyo", "中山":"nakayama", "中京":"chukyo", "京都":"kyoto", "阪神":"hanshin", "小倉":"kokura"};
            const tEnMap = {"芝":"turf", "ダート":"dirt", "障害":"jump"};
            if (vEnMap[data.venue] && tEnMap[data.race_type]) {
                const vEn = vEnMap[data.venue];
                const tEn = tEnMap[data.race_type];
                courseImage.src = `/assets/images/courses/${vEn}_${tEn}_${data.dist_val}.png`;
            }
        }
        courseImage.onerror = () => {
            courseImage.style.display = 'none';
            courseImage.dataset.loadFailed = '1';
            if (courseMapWrap) courseMapWrap.classList.add('fallback');
            if (windSvg) windSvg.setAttribute('viewBox', '0 0 570 400');
            if (window.lastWindData && window.lastWindData.venue === data.venue) {
                renderWindOverlay(window.lastWindData.venue, window.lastWindData.dir, window.lastWindData.speed);
            }
        };
        courseImage.onload = () => {
            courseImage.style.display = 'block';
            courseImage.dataset.loadFailed = '0';
            if (courseMapWrap) courseMapWrap.classList.remove('fallback');
            if (windSvg && courseImage.naturalWidth && courseImage.naturalHeight) {
                windSvg.setAttribute('viewBox', `0 0 ${courseImage.naturalWidth} ${courseImage.naturalHeight}`);
            }
            if (window.lastWindData && window.lastWindData.venue === data.venue) {
                renderWindOverlay(window.lastWindData.venue, window.lastWindData.dir, window.lastWindData.speed);
            }
        };
    }

    const select = document.getElementById('historyHorseSelect');
    select.innerHTML = '<option value="">-- 馬を選択 --</option>';
    globalHorsesData.forEach(h => {
        const opt = document.createElement('option');
        opt.value = h.num;
        opt.textContent = `${h.num} ${h.name}`;
        select.appendChild(opt);
    });
    
    document.getElementById('historyTbody').innerHTML = '';
    if (globalHorsesData.length > 0) {
        select.value = globalHorsesData[0].num;
        updateHistoryTable();
    }
    
    // Store context for past data analysis
    currentRaceContext.venue = data.venue;
    currentRaceContext.track_type = data.race_type;
    currentRaceContext.distance = data.dist_val;
    currentRaceContext.race_class = data.race_class || "不明";
    
    let bText = document.getElementById('babaInfo').textContent;
    let m = bText.match(new RegExp(data.race_type + "[:：]\\s*([^\\s\\(]+)"));
    currentRaceContext.condition = m ? m[1] : "良";
    
    const dClass = document.getElementById('displayRaceClass');
    if (dClass) dClass.textContent = `[${currentRaceContext.race_class}]`;
    const dCond = document.getElementById('displayCondition');
    if (dCond) dCond.textContent = `[${currentRaceContext.condition}]`;
    
    // Restore cached past data if available, otherwise hide the panel
    if (!_tryRestorePastData()) {
        const pmContainer = document.getElementById('pastDataResultsContainer');
        if(pmContainer) pmContainer.style.display = 'none';
        const pmStatus = document.getElementById('pastDataStatus');
        if(pmStatus) pmStatus.textContent = '';
    }
    
    // Notable Sires Rendering (NEW)
    window.lastRaceData = data;
    renderNotableSiresTable(data.notable_sires, `${data.venue} ${data.race_type}${data.dist_val}m`);

    // 4冊分の好走条件まとめ (NEW)
    renderBookData(data);

    // Fetch Wind Data
    fetchWindData(data.venue);

    // SPEC-T73 §2.4: オッズ監視の評価 (統合版のみ。本番Webでは/ev/が存在しないため無視される)
    renderEvSummary(data);
}

async function renderEvSummary(data) {
    let evState;
    try {
        const res = await fetch('../ev/api/state');
        if (!res.ok) throw new Error('ev/api/state not available');
        evState = await res.json();
    } catch (e) {
        return; // 本番Web/未起動時などは静かにスキップ
    }

    let box = document.getElementById('evSummary');
    const races = (evState && evState.races) || [];
    const race = races.find(r => r.venue === data.venue &&
        Number(r.race_num) === Number(data.race_num));

    if (!race) {
        if (box) box.remove();
        return;
    }
    if (!box) {
        box = document.createElement('div');
        box.id = 'evSummary';
        box.style.margin = '6px 0 10px';
        box.style.fontSize = '13px';
        const raceInfoEl = document.getElementById('raceInfo');
        // flex 行 (.status-panel) の外に置く (renderEmbeddedRaceList と同じ理由)
        const panel = raceInfoEl.closest('.status-panel') || raceInfoEl;
        panel.insertAdjacentElement('afterend', box);
    }

    const coverage = race.ml_coverage;
    if (coverage && coverage.ok === false) {
        box.innerHTML = `<div style="font-weight:bold;">オッズ監視の評価</div>` +
            `<div>MLスコア付き馬が不足のためEV対象外 (${coverage.scored}/${coverage.total}頭)</div>`;
        return;
    }

    const horses = (race.horses || []).slice()
        .sort((a, b) => (b.win_prob ?? -1) - (a.win_prob ?? -1));
    const rows = horses.map(h => `<tr>
        <td>${h.num ?? ''}</td>
        <td>${h.name ?? ''}</td>
        <td>${h.odds ?? ''}</td>
        <td>${h.win_prob != null ? (h.win_prob * 100).toFixed(1) + '%' : ''}</td>
        <td>${h.ev != null ? h.ev : ''}</td>
        <td>${h.picked ? '★' : ''}</td>
        <td>${h.place_prob != null ? (h.place_prob * 100).toFixed(1) + '%' : ''}</td>
    </tr>`).join('');

    box.innerHTML = `<div style="font-weight:bold;">オッズ監視の評価</div>
        <table style="border-collapse:collapse;width:100%;">
            <thead><tr>
                <th style="text-align:left;">馬番</th><th style="text-align:left;">馬名</th>
                <th style="text-align:left;">単勝</th><th style="text-align:left;">CL勝率</th>
                <th style="text-align:left;">EV</th><th style="text-align:left;">EV対象</th>
                <th style="text-align:left;">複勝率β</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
}

function renderBookData(data) {
    const sireBuysellTbody = document.getElementById('sireBuysellTbody');
    if (sireBuysellTbody) {
        const horses = data.horses || [];
        const rows = [];
        horses.forEach(h => {
            (h.sire_buysell || []).forEach(bs => {
                rows.push(`<tr><td>${h.num}</td><td>${h.name}</td><td>${h.sire || '-'}</td><td>${bs.kubun}</td><td>${bs.condition}</td></tr>`);
            });
        });
        sireBuysellTbody.innerHTML = rows.length > 0 ? rows.join('') : '<tr><td colspan="5">データなし</td></tr>';
    }
}

function renderNotableSiresTable(sires, title) {
    const tbody = document.getElementById('sireTbody');
    const tabTitle = document.getElementById('sireTabTitle');
    if (!tbody) return;

    if (tabTitle) tabTitle.textContent = `${title} 注目産駒データ`;

    if (!sires || sires.length === 0) {
        let debugHtml = '<p style="color:red; font-size:10px; margin-top:10px;">Debug: Data not found</p>';
        if (window.lastRaceData && window.lastRaceData.debug_sire) {
            const d = window.lastRaceData.debug_sire;
            debugHtml += `<p style="font-size:9px; color:gray; text-align:left;">
                Path: ${d.attempted_path}<br>
                Exists: ${d.exists}<br>
                Dir Exists: ${d.dir_exists}<br>
                API Contents: ${JSON.stringify(d.api_contents || [])}
            </p>`;
        }
        tbody.innerHTML = `<tr><td colspan="5">このコースの注目産駒データはありません。${debugHtml}</td></tr>`;
        return;
    }

    tbody.innerHTML = sires.map(s => {
        let rankClass = "";
        if (s.rank === 1) rankClass = "sire-rank-1";
        else if (s.rank <= 5) rankClass = "sire-rank-2-5";
        else rankClass = "sire-rank-6-10";

        return `<tr>
            <td>${s.rank}</td>
            <td style="text-align: left; font-weight: bold;" class="${rankClass}">${s.name}</td>
            <td>${s.win_rate}</td>
            <td>${s.quinella_rate}</td>
            <td>${s.show_rate}</td>
        </tr>`;
    }).join('');
}

// ---- T76 wind pure functions (begin) ----
const COURSE_DIRECTION = {
    "札幌": { lat: 43.075, lon: 141.275, dir: 110 },
    "函館": { lat: 41.791, lon: 140.781, dir: 320 },
    "福島": { lat: 37.766, lon: 140.457, dir: 160 },
    "新潟": { lat: 37.954, lon: 139.172, dir: 250 },
    "中山": { lat: 35.733, lon: 139.957, dir: 140 },
    "東京": { lat: 35.662, lon: 139.485, dir: 290 },
    "中京": { lat: 35.068, lon: 136.988, dir: 310 },
    "京都": { lat: 34.908, lon: 135.722, dir: 160 },
    "阪神": { lat: 34.779, lon: 135.361, dir: 70 },
    "小倉": { lat: 33.834, lon: 130.875, dir: 340 }
};

// SPEC-T76 §1.1: ホームストレッチの画面上の向き (θ_s)。左回り=0 / 右回り=180。
const COURSE_HANDEDNESS = {
    "東京": "left", "中京": "left", "新潟": "left",
    "中山": "right", "阪神": "right", "京都": "right", "函館": "right",
    "札幌": "right", "福島": "right", "小倉": "right"
};

function _t76Mod360(x) {
    return ((x % 360) + 360) % 360;
}

// diff<=45: 向かい風(head) / diff>=135: 追い風(tail) / それ以外: 横風(cross)
// (straight = ホームストレッチ視点、backstretch = 向こう正面視点で常に逆になる)
function classifyWindVsCourse(windFromDeg, courseDir) {
    let diff = Math.abs(windFromDeg - courseDir);
    if (diff > 180) diff = 360 - diff;

    let straight, backstretch;
    if (diff <= 45) {
        straight = "head"; backstretch = "tail";
    } else if (diff >= 135) {
        straight = "tail"; backstretch = "head";
    } else {
        straight = "cross"; backstretch = "cross";
    }
    return { diff, straight, backstretch };
}

const WIND_SPEED_CATEGORIES = [
    { max: 0.3, key: "calm", label: "静穏", color: "#9e9e9e" },
    { max: 10, key: "light", label: "微風", color: "#4fc3f7" },
    { max: 15, key: "moderate", label: "やや強い風", color: "#ffd54f" },
    { max: 20, key: "strong", label: "強い風", color: "#ff9800" },
    { max: 30, key: "very_strong", label: "非常に強い風", color: "#f44336" },
    { max: Infinity, key: "violent", label: "猛烈な風", color: "#d500f9" }
];

function windSpeedCategory(speedMs) {
    for (const cat of WIND_SPEED_CATEGORIES) {
        if (speedMs < cat.max) {
            return { key: cat.key, label: cat.label, color: cat.color };
        }
    }
    const last = WIND_SPEED_CATEGORIES[WIND_SPEED_CATEGORIES.length - 1];
    return { key: last.key, label: last.label, color: last.color };
}

// SPEC-T76 §1.1: θ(B) = θ_s + (B - dir) (mod 360)
function computeWindScreenAngles(venue, windFromDeg) {
    const course = COURSE_DIRECTION[venue];
    const handed = COURSE_HANDEDNESS[venue];
    if (!course || !handed) return null;

    const thetaS = handed === "left" ? 0 : 180;
    const dir = course.dir;
    const theta = (B) => _t76Mod360(thetaS + (B - dir));

    const bTo = _t76Mod360(windFromDeg + 180);
    const thetaTo = theta(bTo);
    const thetaNorth = theta(0);
    const cls = classifyWindVsCourse(windFromDeg, dir);

    return {
        thetaTo, thetaNorth, handed,
        straight: cls.straight, backstretch: cls.backstretch
    };
}
// ---- T76 wind pure functions (end) ----

window.JRA_WIND = {
    COURSE_HANDEDNESS, classifyWindVsCourse, windSpeedCategory, computeWindScreenAngles
};

const WIND_TERM_JA = { head: "向かい風", tail: "追い風", cross: "横風" };
const WIND_EFFECT_TEXT_JA = {
    head: "直線が向かい風となるため、逃げ・先行馬が有利になる傾向があります。",
    tail: "直線が追い風となるため、差し・追込馬が有利になる傾向があります。",
    cross: "直線は横風となるため、内外で影響が変わる可能性があります。"
};

function getWindDirectionString(deg) {
    const directions = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"];
    return directions[Math.round(deg / 22.5)];
}

function getWindSpeedTerm(speed) {
    return windSpeedCategory(speed).label;
}

function checkWindEffectHtml(windDir, courseDir) {
    const cls = classifyWindVsCourse(windDir, courseDir);
    const straightWind = WIND_TERM_JA[cls.straight];
    const backstretchWind = WIND_TERM_JA[cls.backstretch];
    const effect = WIND_EFFECT_TEXT_JA[cls.straight];

    return `向こう正面は${backstretchWind}、直線は${straightWind}となります。<br><span style="color:#ffcc00; font-weight:bold;">${effect}</span>`;
}

async function fetchWindData(venue) {
    const windDisplay = document.getElementById('windDataDisplay');
    if (!windDisplay) return;

    const course = COURSE_DIRECTION[venue];
    if (!course) {
        windDisplay.textContent = `風データ: ${venue}の緯度経度情報がありません`;
        clearWindOverlay(`${venue}の風データはありません`);
        return;
    }

    windDisplay.textContent = "風データを取得中...";
    try {
        const res = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${course.lat}&longitude=${course.lon}&current_weather=true&windspeed_unit=ms`);
        const result = await res.json();
        if (result.current_weather) {
            const w = result.current_weather;
            const dirStr = getWindDirectionString(w.winddirection);
            const speedTerm = getWindSpeedTerm(w.windspeed);
            const effectHtml = checkWindEffectHtml(w.winddirection, course.dir);

            windDisplay.innerHTML = `<strong>リアルタイム風力データ (${venue}):</strong> ${dirStr}からの風 (${w.winddirection}°), ${speedTerm} (${w.windspeed}m/s)<br>${effectHtml}`;

            window.lastWindData = { venue, dir: w.winddirection, speed: w.windspeed, time: w.time };
            renderWindOverlay(venue, w.winddirection, w.windspeed);
        } else {
            clearWindOverlay();
        }
    } catch(e) {
        windDisplay.textContent = "風データの取得に失敗しました。";
        clearWindOverlay();
    }
}

// ─── SPEC-T76 §1.2/1.3: コース図オーバーレイ (風向・風速の矢印表示) ───────────

const WIND_LINE_STYLE = {
    calm: { count: 0, width: 0, opacity: 0 },
    light: { count: 5, width: 2, opacity: 0.65 },
    moderate: { count: 7, width: 3, opacity: 0.62 },
    strong: { count: 9, width: 4, opacity: 0.7 },
    very_strong: { count: 11, width: 5, opacity: 0.78 },
    violent: { count: 11, width: 5, opacity: 0.8 }
};

const SVG_NS = "http://www.w3.org/2000/svg";

function _windSvgEl() {
    return document.getElementById('windOverlay');
}

function _windLegendEl() {
    return document.getElementById('windLegend');
}

function _svgViewBoxSize(svg) {
    const vb = (svg.getAttribute('viewBox') || '0 0 570 400').split(/\s+/).map(Number);
    return { width: vb[2] || 570, height: vb[3] || 400 };
}

function _makeSvgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(k => el.setAttribute(k, attrs[k]));
    return el;
}

// SVGはインライン (同一オリジンDOM) なので実測できる。text要素は事前に
// 描画対象のSVGへappendしてから呼ぶこと (未接続の要素は0を返す環境がある)。
// 失敗/0の場合のみ CJK=1em・ASCII=0.6em の概算にフォールバックする。
function _measureTextWidth(textEl, fontSize) {
    try {
        const w = textEl.getComputedTextLength();
        if (w && isFinite(w) && w > 0) return w;
    } catch (e) { /* フォールバックへ */ }
    const s = textEl.textContent || '';
    let width = 0;
    for (const ch of s) {
        width += /[　-ヿ㐀-鿿＀-￯]/.test(ch) ? fontSize : fontSize * 0.6;
    }
    return width;
}

// テキストを先にcontainerへappendしてgetComputedTextLength()で幅を測り、
// パディング込みの背景rectをテキストの直前に挿入する (paint順でrectが下)。
function _appendChipWithMeasuredBg(container, textEl, fontSize, padX, rectAttrs) {
    container.appendChild(textEl);
    const textWidth = _measureTextWidth(textEl, fontSize);
    const rect = _makeSvgEl('rect', rectAttrs(textWidth + padX * 2));
    container.insertBefore(rect, textEl);
    return rect;
}

function clearWindOverlay(message) {
    const svg = _windSvgEl();
    if (svg) svg.innerHTML = '';
    const legend = _windLegendEl();
    if (legend) legend.textContent = message || '風データ取得失敗';
}

// コース図が読めない場合の簡略図 (SPEC §1.2 フォールバック)
function _drawCourseFallback(svg, venue, width, height) {
    const cx = width / 2, cy = height / 2;
    const rx = width * 0.42, ry = height * 0.32;

    svg.appendChild(_makeSvgEl('ellipse', {
        cx, cy, rx, ry, fill: 'none', stroke: '#6b8f6b', 'stroke-width': 10, opacity: 0.5
    }));
    svg.appendChild(_makeSvgEl('ellipse', {
        cx, cy, rx: rx * 0.72, ry: ry * 0.62, fill: 'none', stroke: '#4a7a4a', 'stroke-width': 4, opacity: 0.6
    }));

    const standW = width * 0.5, standH = height * 0.1;
    svg.appendChild(_makeSvgEl('rect', {
        x: cx - standW / 2, y: cy + ry + 6, width: standW, height: standH,
        fill: 'rgba(120,120,120,0.5)', rx: 4
    }));
    const standLabel = _makeSvgEl('text', {
        x: cx, y: cy + ry + 6 + standH / 2 + 4, 'text-anchor': 'middle',
        'font-size': 11, fill: '#eee'
    });
    standLabel.textContent = 'スタンド';
    svg.appendChild(standLabel);

    const handed = COURSE_HANDEDNESS[venue];
    const goalX = handed === 'right' ? cx - rx : cx + rx;
    const goalY = cy + ry * 0.15;
    svg.appendChild(_makeSvgEl('polygon', {
        points: `${goalX - 6},${goalY - 8} ${goalX + 6},${goalY} ${goalX - 6},${goalY + 8}`,
        fill: '#ffd54f'
    }));
}

// SPEC-T76 §1.2: コース図の上に風向・風速の矢印オーバーレイを一括描画する。
function renderWindOverlay(venue, windFromDeg, windSpeedMs) {
    const svg = _windSvgEl();
    if (!svg) return;
    svg.innerHTML = '';

    const angles = computeWindScreenAngles(venue, windFromDeg);
    if (!angles) {
        clearWindOverlay(`${venue}は未対応のコースです`);
        return;
    }

    const { width, height } = _svgViewBoxSize(svg);
    const cx = width / 2, cy = height / 2;
    const cat = windSpeedCategory(windSpeedMs);
    const courseImage = document.getElementById('courseLayoutImage');
    const usingFallback = !!(courseImage && courseImage.dataset.loadFailed === '1');

    let legendPrefix = '';
    if (usingFallback) {
        _drawCourseFallback(svg, venue, width, height);
        legendPrefix = 'コース図なし (簡略図)。 ';
    }

    // 1) 風の流線 (静穏でなければ)
    const style = WIND_LINE_STYLE[cat.key] || WIND_LINE_STYLE.calm;

    const defs = _makeSvgEl('defs', {});
    const clipId = 'windClip';
    const clipPath = _makeSvgEl('clipPath', { id: clipId });
    clipPath.appendChild(_makeSvgEl('rect', { x: 0, y: 0, width, height }));
    defs.appendChild(clipPath);

    // 矢頭は線幅に比例させる (細い微風の矢印だと8x8固定では向きが読めないため)
    const markerId = 'windArrowHead';
    const mSize = 8 + 2.5 * style.width;
    const marker = _makeSvgEl('marker', {
        id: markerId, markerWidth: mSize, markerHeight: mSize,
        refX: mSize * 0.8, refY: mSize / 2,
        orient: 'auto', markerUnits: 'userSpaceOnUse'
    });
    marker.appendChild(_makeSvgEl('path', {
        d: `M0,0 L${mSize},${mSize / 2} L0,${mSize} Z`, fill: cat.color
    }));
    defs.appendChild(marker);
    svg.appendChild(defs);

    if (style.count > 0) {
        const diag = Math.sqrt(width * width + height * height);
        const spacing = height / (style.count + 1);
        const period = Math.max(0.6, 4 - windSpeedMs * 0.25);
        // 約110 viewBox単位ごとに頂点を打ち、marker-midで矢頭を線に沿って繰り返す
        const segLen = 110;
        const numSegs = Math.max(2, Math.round(diag / segLen));

        const g = _makeSvgEl('g', {
            transform: `rotate(${angles.thetaTo} ${cx} ${cy})`,
            'clip-path': `url(#${clipId})`
        });

        for (let i = 0; i < style.count; i++) {
            const y = spacing * (i + 1);
            const x1 = cx - diag / 2;
            let d = `M${x1},${y}`;
            for (let s = 1; s <= numSegs; s++) {
                const x = x1 + (diag * s / numSegs);
                d += ` L${x},${y}`;
            }
            const path = _makeSvgEl('path', {
                d,
                fill: 'none',
                stroke: cat.color,
                'stroke-width': style.width,
                'stroke-linecap': 'round',
                opacity: style.opacity,
                'stroke-dasharray': '18 14',
                'marker-mid': `url(#${markerId})`,
                'marker-end': `url(#${markerId})`
            });
            path.classList.add('wind-flow-line');
            path.style.animationDuration = `${period}s`;
            g.appendChild(path);
        }
        svg.appendChild(g);
    } else {
        const chip = _makeSvgEl('g', {});
        svg.appendChild(chip);
        const text = _makeSvgEl('text', {
            x: cx, y: cy + 4, 'text-anchor': 'middle', 'font-size': 13,
            'font-weight': 'bold', fill: '#fff'
        });
        text.textContent = '静穏';
        _appendChipWithMeasuredBg(chip, text, 13, 12, (w) => ({
            x: cx - w / 2, y: cy - 13, width: w, height: 26, rx: 10,
            fill: 'rgba(0,0,0,0.55)', stroke: '#9e9e9e'
        }));
    }

    // 2) 方位記号 (N)
    const ncx = width - 34, ncy = 34, nr = 22;
    svg.appendChild(_makeSvgEl('circle', {
        cx: ncx, cy: ncy, r: nr, fill: 'rgba(0,0,0,.55)', stroke: '#fff', 'stroke-width': 1.5
    }));
    const nGroup = _makeSvgEl('g', { transform: `rotate(${angles.thetaNorth} ${ncx} ${ncy})` });
    nGroup.appendChild(_makeSvgEl('polygon', {
        points: `${ncx},${ncy - nr + 5} ${ncx - 5},${ncy} ${ncx + 5},${ncy}`,
        fill: '#ff5252'
    }));
    nGroup.appendChild(_makeSvgEl('line', {
        x1: ncx, y1: ncy, x2: ncx, y2: ncy + nr - 6, stroke: '#fff', 'stroke-width': 2
    }));
    svg.appendChild(nGroup);
    const nText = _makeSvgEl('text', {
        x: ncx, y: ncy + nr + 13, 'text-anchor': 'middle', 'font-size': 11,
        'font-weight': 'bold', fill: '#fff'
    });
    nText.textContent = 'N';
    svg.appendChild(nText);

    // 3) 直線 / 向こう正面バッジ (getComputedTextLength()で実測してrectを合わせる)
    const badgeColor = { head: '#ff9800', tail: '#4fc3f7', cross: '#bdbdbd' };
    const drawBadge = (xPct, yPct, label, key) => {
        const text = `${label}: ${WIND_TERM_JA[key]}`;
        const bx = width * xPct, by = height * yPct;
        const g = _makeSvgEl('g', {});
        svg.appendChild(g);
        const t = _makeSvgEl('text', {
            x: bx, y: by + 4, 'text-anchor': 'middle', 'font-size': 13,
            'font-weight': 'bold', fill: '#fff'
        });
        t.textContent = text;
        _appendChipWithMeasuredBg(g, t, 13, 12, (w) => ({
            x: bx - w / 2, y: by - 13, width: w, height: 26, rx: 10,
            fill: badgeColor[key], opacity: 0.88
        }));
    };
    drawBadge(0.5, 0.71, '直線', angles.straight);
    drawBadge(0.5, 0.09, '向こう正面', angles.backstretch);

    // 4) 風速チップ (左上)
    const dirStr = getWindDirectionString(windFromDeg);
    const speedText = `🌬 ${dirStr} ${windSpeedMs.toFixed(1)} m/s ${cat.label}`;
    const speedGroup = _makeSvgEl('g', {});
    svg.appendChild(speedGroup);
    const speedTextEl = _makeSvgEl('text', {
        x: 6, y: 22, 'text-anchor': 'middle', 'font-size': 12,
        'font-weight': 'bold', fill: '#fff'
    });
    speedTextEl.textContent = speedText;
    _appendChipWithMeasuredBg(speedGroup, speedTextEl, 12, 12, (w) => {
        speedTextEl.setAttribute('x', 6 + w / 2);
        // 白いコース図の上でも読めるよう暗色地 + 風速階級色の縁取り (レビュー時の手直し)
        return { x: 6, y: 6, width: w, height: 24, rx: 8, fill: 'rgba(0,0,0,0.6)', stroke: cat.color, 'stroke-width': 1.5 };
    });

    // 5) 凡例
    const legend = _windLegendEl();
    if (legend) legend.textContent = legendPrefix + '矢印 = 風の吹いていく向き / 本数と色 = 強さ / N = 北';
}

let activeSortCol = null;  // '回収スコア' | '的中スコア' | '複勝率β' | null(馬番順)

function setupScoreSortHeader() {
    const fields = { '回収スコア': 'score', '的中スコア': 'score_ml', '複勝率β': 'place_prob' };
    const ths = document.querySelectorAll('#horsesTable thead th');
    ths.forEach(th => {
        const label = th.textContent.replace('▼', '').trim();
        if (!(label in fields)) return;
        if (th.dataset.sortBound) return;
        th.dataset.sortBound = '1';
        th.style.cursor = 'pointer';
        th.title = 'クリックでスコア順⇔馬番順を切替';
        th.onclick = () => {
            activeSortCol = (activeSortCol === label) ? null : label;
            document.querySelectorAll('#horsesTable thead th').forEach(t => {
                const l = t.textContent.replace('▼', '').trim();
                if (l in fields) t.textContent = (l === activeSortCol) ? l + '▼' : l;
            });
            const sorted = [...globalHorsesData];
            if (activeSortCol) {
                const f = fields[activeSortCol];
                sorted.sort((a, b) => (b[f] ?? -999) - (a[f] ?? -999));
            } else {
                sorted.sort((a, b) => a.num - b.num);
            }
            renderHorsesTable(sorted);
        };
    });
}

function renderHorsesTable(horses) {
    const tbody = document.getElementById('horsesTbody');
    tbody.innerHTML = '';
    setupScoreSortHeader();

    // スコア上位3頭 (nullは除外)
    const scoreTop3 = [...horses]
        .filter(x => x.score !== null && x.score !== undefined)
        .sort((a, b) => b.score - a.score)
        .slice(0, 3)
        .map(x => x.num);
    const mlTop3 = [...horses]
        .filter(x => x.score_ml !== null && x.score_ml !== undefined)
        .sort((a, b) => b.score_ml - a.score_ml)
        .slice(0, 3)
        .map(x => x.num);

    horses.forEach(h => {
        const tr = document.createElement('tr');
        tr.dataset.horseNum = h.num;
        updateRowMarkStyle(tr, h.num); // 既存の印を反映

        const tdWaku = document.createElement('td');
        tdWaku.textContent = h.waku || h.w_num;
        tdWaku.className = `waku-${h.waku || h.w_num}`;

        // Other Cols
        const isDebutMl = h.score_ml == null && (h.score_ml_details || []).some(x => x.includes('初出走'));
        const isDebutPlace = h.place_prob == null && (h.place_prob_details || []).some(x => x.includes('初出走'));
        const cols = [
            h.num, h.grade || '-', (h.score ?? '-'), (isDebutMl ? '初出走' : (h.score_ml ?? '-')),
            (isDebutPlace ? '初出走' : (h.place_prob != null ? (h.place_prob * 100).toFixed(1) + '%' : '-')),
            h.odds, h.pop || '-', h.iv, h.dist_diff || '-',
            h.name, h.sex_age, h.kyakushitsu, h.kg, h.jock || h.jockey,
            h.affi, h.sire, h.bms
        ];

        tr.appendChild(tdWaku);
        cols.forEach((val, idx) => {
            const td = document.createElement('td');

            if (idx === 9) { // idx 9 is h.name
                td.style.textAlign = 'left';
                const expandBtn = document.createElement('button');
                expandBtn.textContent = '+';
                expandBtn.className = 'expand-history-btn';
                expandBtn.style.marginRight = '8px';
                
                expandBtn.onclick = (e) => {
                    e.stopPropagation();
                    const nextTr = tr.nextElementSibling;
                    if (nextTr && nextTr.classList.contains('history-row')) {
                        if (nextTr.style.display === 'none') {
                            nextTr.style.display = window.innerWidth <= 768 ? 'block' : 'table-row';
                            expandBtn.textContent = '-';
                        } else {
                            nextTr.style.display = 'none';
                            expandBtn.textContent = '+';
                        }
                    } else {
                        const histRow = document.createElement('tr');
                        histRow.className = 'history-row';
                        const histTd = document.createElement('td');
                        histTd.colSpan = 19; // 枠+17列+印列
                        histTd.innerHTML = buildMiniHistoryTable(h.hist);
                        histRow.appendChild(histTd);
                        tr.after(histRow);
                        expandBtn.textContent = '-';
                    }
                };
                td.appendChild(expandBtn);
                
                const nameSpan = document.createElement('span');
                nameSpan.textContent = val;
                td.appendChild(nameSpan);
            } else {
                td.textContent = val;
            }
            
            if (idx === 1 && val === '◎') {
                td.classList.add('grade-tooltip-target');
                const tooltipSpan = document.createElement('span');
                tooltipSpan.className = 'tooltip-text';
                tooltipSpan.innerHTML = h.ultra_details && h.ultra_details.length > 0
                    ? h.ultra_details.join('<br>')
                    : '詳細データなし';
                td.appendChild(tooltipSpan);
            }

            // 回収スコア列 (idx 2): 内訳tooltip + 上位3頭ハイライト
            if (idx === 2) {
                if (h.score_details && h.score_details.length > 0) {
                    td.classList.add('grade-tooltip-target');
                    const tooltipSpan = document.createElement('span');
                    tooltipSpan.className = 'tooltip-text';
                    tooltipSpan.innerHTML = h.score_details.join('<br>');
                    td.appendChild(tooltipSpan);
                }
                const rankIdx = scoreTop3.indexOf(h.num);
                if (rankIdx >= 0) td.classList.add(`score-top-${rankIdx + 1}`);
            }

            // 的中スコア列 (idx 3): WIN5アプリと同じMLスコア (conditional logit)
            if (idx === 3) {
                if (h.score_ml_details && h.score_ml_details.length > 0) {
                    td.classList.add('grade-tooltip-target');
                    const tooltipSpan = document.createElement('span');
                    tooltipSpan.className = 'tooltip-text';
                    tooltipSpan.innerHTML = h.score_ml_details.join('<br>');
                    td.appendChild(tooltipSpan);
                }
                const rankIdx = mlTop3.indexOf(h.num);
                if (rankIdx >= 0) td.classList.add(`score-top-${rankIdx + 1}`);
            }

            // 複勝率β列 (idx 4): 市場非依存モデルの参考表示のみ
            if (idx === 4 && h.place_prob_details && h.place_prob_details.length > 0) {
                td.classList.add('grade-tooltip-target');
                const tooltipSpan = document.createElement('span');
                tooltipSpan.className = 'tooltip-text';
                tooltipSpan.innerHTML = h.place_prob_details.join('<br>');
                td.appendChild(tooltipSpan);
            }

            // Sire Ranking Highlight (idx 15 corresponds to h.sire)
            if (idx === 15 && h.sire_rank) {
                if (h.sire_rank === 1) td.classList.add('sire-rank-1');
                else if (h.sire_rank <= 5) td.classList.add('sire-rank-2-5');
                else if (h.sire_rank <= 10) td.classList.add('sire-rank-6-10');
            }

            tr.appendChild(td);
        });

        // ── 印（メモ）列 ──
        const tdMark = document.createElement('td');
        tdMark.className = 'mark-cell';
        tdMark.dataset.num = h.num;
        refreshMarkCell(tdMark, h.num);
        tr.appendChild(tdMark);

        tbody.appendChild(tr);
    });
}

function buildMiniHistoryTable(hist) {
    if (!hist || hist.length === 0) {
        return '<div style="padding: 10px; text-align: center; color: var(--text-muted);">過去データなし</div>';
    }
    
    let html = `<div class="mini-history-container">`;
    const labels = ["前走", "2走前", "3走前", "4走前"];
    
    hist.forEach((hInfo, idx) => {
        if(idx > 2) return; // limit to 3 races max for UI space
        
        const dateMatch = hInfo.raw ? hInfo.raw.match(/(\d{4}年\d+月\d+日)/) : null;
        const dateStr = dateMatch ? dateMatch[1] : '-';
        const cond = [hInfo.course, hInfo.condition].filter(Boolean).join(' ') || '-';
        
        html += `<div class="mini-history-row">
            <div class="mh-header"><strong>${labels[idx]}</strong> <span>${dateStr} ${hInfo.place || '-'} ${cond}</span></div>
            <div class="mh-body">
                <div><span>レース:</span> ${hInfo.race_name || '-'} (${hInfo.total || '-'}頭 ${hInfo.pop_rank || '-'}人)</div>
                <div><span>着順:</span> <strong style="color:var(--text-main);">${hInfo.rank || '-'}</strong></div>
                <div><span>タイム:</span> ${hInfo.run_time || '-'} (上${hInfo.agari_rank || '-'})</div>
                <div><span>通過順:</span> ${hInfo.corners || '-'}</div>
                <div><span>騎手/斤量:</span> ${hInfo.jockey || '-'} ${hInfo.kinryo || '-'}</div>
                <div><span>馬体重:</span> ${hInfo.weight || '-'}</div>
            </div>
        </div>`;
    });
    html += `</div>`;
    return html;
}

function renderMatrix(matrixData, currentVenue) {
    const container = document.getElementById('matrixContainer');
    container.innerHTML = '';
    if(!matrixData || matrixData.length === 0) {
        container.innerHTML = '<div class="empty-state">マトリックスデータなし</div>';
        return;
    }
    
    matrixData.forEach(venueData => {
        const row = document.createElement('div');
        row.className = 'matrix-row';
        
        const lLabel = document.createElement('div');
        lLabel.className = 'matrix-label';
        const labelText = venueData.text;
        
        if(labelText.includes('中山')) lLabel.style.background = 'var(--v-nakayama)';
        else if(labelText.includes('京都')) lLabel.style.background = 'var(--v-kyoto)';
        else if(labelText.includes('東京')) lLabel.style.background = 'var(--v-tokyo)';
        else if(labelText.includes('阪神')) lLabel.style.background = 'var(--v-hanshin)';
        else lLabel.style.background = 'var(--v-others)';
        lLabel.textContent = labelText;
        
        row.appendChild(lLabel);
        
        if(venueData.races && venueData.races.length > 0) {
            venueData.races.sort((a,b) => a.r - b.r);
            venueData.races.forEach(raceItem => {
                const btn = document.createElement('button');
                btn.className = 'r-btn';
                
                if (raceCache.hasOwnProperty(raceItem.url)) {
                    if (raceCache[raceItem.url]) {
                        btn.classList.add('has-star');
                    } else {
                        btn.classList.add('visited-no-star');
                    }
                }
                
                btn.textContent = `${raceItem.r}R`;
                btn.onclick = () => {
                    document.getElementById('urlInput').value = raceItem.url;
                    startScraping();
                };
                row.appendChild(btn);
            });
        }
        
        container.appendChild(row);
    });
}

function updateHistoryTable() {
    const num = document.getElementById('historyHorseSelect').value;
    const tbody = document.getElementById('historyTbody');
    tbody.innerHTML = '';
    
    if(!num) return;
    const horse = globalHorsesData.find(h => String(h.num) === String(num));
    if(!horse || !horse.hist || horse.hist.length === 0) return;
    
    const labels = ["前走", "2走前", "3走前", "4走前"];
    
    horse.hist.forEach((hInfo, idx) => {
        if(idx > 3) return;
        
        const tr = document.createElement('tr');
        
        const dateMatch = hInfo.raw.match(/(\d{4}年\d+月\d+日)/);
        const dateStr = dateMatch ? dateMatch[1] : '-';
        
        const cols = [
            labels[idx], dateStr, hInfo.place, hInfo.course, hInfo.condition,
            hInfo.kinryo, hInfo.jockey, hInfo.race_name, hInfo.corners, hInfo.agari_rank,
            hInfo.run_time, hInfo.pop_rank, hInfo.rank, hInfo.total, hInfo.weight
        ];
        
        cols.forEach(val => {
            const td = document.createElement('td');
            td.textContent = val || '-';
            tr.appendChild(td);
        });
        
        tbody.appendChild(tr);
    });
}

async function runAiPrediction() {
    if(globalHorsesData.length === 0) {
        alert("レースデータがありません。先に解析を実行してください。");
        return;
    }
    
    document.getElementById('aiStatus').textContent = "AI予想を生成中... (数秒かかります)";
    document.getElementById('runAiBtn').disabled = true;
    
    // Construct Prompt
    const raceInfo = document.getElementById('raceInfo').textContent;
    const babaInfo = document.getElementById('babaInfo').textContent;
    
    let prompt = `以下の競馬のレース情報と出走馬データをもとに、プロの競馬予想家としてAIレース予想を行ってください。\n`;
    prompt += `予想は以下の4つのファクターに分けて詳細に分析し、最後にそれらを統合した「総合予想（印と買い目、見解）」を出力してください。\n`;
    prompt += `1. 血統\n2. レース展開\n3. コース適正\n4. 馬場適正\n\n`;
    prompt += `■ レース情報\n${raceInfo}\n\n`;
    prompt += `■ 当日の馬場情報（参考）\n${babaInfo}\n\n`;
    prompt += `■ 出走馬データ\n`;
    
    globalHorsesData.forEach(h => {
        prompt += `馬番${h.num} ${h.name} (オッズ:${h.odds}, 人気:${h.pop}, 間隔:${h.iv})\n`;
        prompt += `  ∟ 判定:${h.grade}, 性齢:${h.sex_age}, 斤量:${h.kg}, 騎手:${h.jock || h.jockey}, 所属:${h.affi}\n`;
        prompt += `  ∟ 血統 - 父:${h.sire}, 母父:${h.bms}\n`;
    });
    
    try {
        const pwd = document.getElementById('aiPassword') ? document.getElementById('aiPassword').value : "";
        const res = await fetch('api/ai_predict', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: prompt, password: pwd })
        });
        const data = await res.json();
        
        if(data.error) throw new Error(data.error);
        
        document.getElementById('aiResult').textContent = data.result;
        document.getElementById('aiStatus').textContent = "完了しました。";
    } catch(err) {
        console.error(err);
        document.getElementById('aiResult').textContent = "エラーが発生しました:\n" + err.message;
        document.getElementById('aiStatus').textContent = "エラー";
    } finally {
        document.getElementById('runAiBtn').disabled = false;
    }
}

function showLoading(text) {
    document.getElementById('loadingOverlay').classList.remove('hidden');
    if(text) document.getElementById('loadingText').textContent = text;
}
function hideLoading() {
    document.getElementById('loadingOverlay').classList.add('hidden');
}

function _buildPastDataUrl() {
    const matchClass = document.getElementById('matchClassCheckbox').checked;
    const matchCond  = document.getElementById('matchConditionCheckbox').checked;
    let url = `api/past_data?place=${encodeURIComponent(currentRaceContext.venue)}&track_type=${encodeURIComponent(currentRaceContext.track_type)}&distance=${currentRaceContext.distance}`;
    if (matchCond && currentRaceContext.condition)  url += `&condition=${encodeURIComponent(currentRaceContext.condition)}`;
    if (matchClass && currentRaceContext.race_class) url += `&race_class=${encodeURIComponent(currentRaceContext.race_class)}`;
    return url;
}

function _showPastData(data) {
    const matchClass = document.getElementById('matchClassCheckbox').checked;
    const matchCond  = document.getElementById('matchConditionCheckbox').checked;
    const condLabel  = matchCond  ? `[${currentRaceContext.condition}]`   : "[馬場状態: 不問]";
    const classLabel = matchClass ? ` [${currentRaceContext.race_class}]` : " [クラス: 不問]";
    const lbl = document.getElementById('matchStatusLabel');
    if (lbl) lbl.textContent = `${currentRaceContext.venue} ${currentRaceContext.track_type} ${currentRaceContext.distance}m ${condLabel}${classLabel}`;
    renderPastDataStats(data.results, 'res');
    document.getElementById('pastDataResultsContainer').style.display = 'flex';
    const st = document.getElementById('pastDataStatus');
    if (st) st.textContent = '集計完了';
}

function _tryRestorePastData() {
    if (!currentRaceContext.venue) return false;
    const apiUrl = _buildPastDataUrl();
    if (!pastDataCache[apiUrl]) return false;
    _showPastData(pastDataCache[apiUrl]);
    return true;
}

async function fetchPastData() {
    if(!currentRaceContext.venue) {
        alert("レースデータがありません。先に解析を実行してください。");
        return;
    }
    const btn    = document.getElementById('runPastDataBtn');
    const status = document.getElementById('pastDataStatus');

    const apiUrl = _buildPastDataUrl();

    // キャッシュがあればAPIを呼ばず即表示
    if (pastDataCache[apiUrl]) {
        _showPastData(pastDataCache[apiUrl]);
        return;
    }

    btn.disabled = true;
    status.textContent = "データ集計中...";
    document.getElementById('pastDataResultsContainer').style.display = 'none';

    try {
        const res = await fetch(apiUrl);
        const data = await res.json();
        if(data.error) throw new Error(data.error);

        pastDataCache[apiUrl] = data;
        _showPastData(data);
    } catch(err) {
        status.textContent = "エラー: " + err.message;
    } finally {
        btn.disabled = false;
    }
}

function renderPastDataStats(stats, prefix) {
    if(!stats) {
         document.getElementById(prefix+'Entries').textContent = "0";
         if(document.getElementById(prefix+'UmabanTbody')) document.getElementById(prefix+'UmabanTbody').innerHTML = '<tr><td colspan="3">データなし</td></tr>';
         if(document.getElementById(prefix+'WakuTbody')) document.getElementById(prefix+'WakuTbody').innerHTML = '<tr><td colspan="3">データなし</td></tr>';
         if(document.getElementById(prefix+'KyakuTbody')) document.getElementById(prefix+'KyakuTbody').innerHTML = '<tr><td colspan="3">データなし</td></tr>';
         if(document.getElementById(prefix+'JockeyTbody')) document.getElementById(prefix+'JockeyTbody').innerHTML = '<tr><td colspan="3">データなし</td></tr>';
         if(document.getElementById(prefix+'WeightTbody')) document.getElementById(prefix+'WeightTbody').innerHTML = '<tr><td colspan="3">データなし</td></tr>';
         return;
    }
    
    document.getElementById(prefix+'Entries').textContent = stats.exact_races + "レース (" + stats.total_entries + "頭)";
    document.getElementById(prefix+'AvgTime').textContent = stats.avg_time;
    document.getElementById(prefix+'AvgAgari').textContent = stats.avg_agari;

    const buildTrs = (arr, cols, highlight = false) => {
        if(!arr || arr.length === 0) return '<tr><td colspan="3">データなし</td></tr>';
        
        let rankMap = new Map();
        if (highlight) {
            let sorted = [...arr].sort((a, b) => {
                let wDiff = (parseFloat(b.win_rate) || 0) - (parseFloat(a.win_rate) || 0);
                if (wDiff !== 0) return wDiff;
                return (parseFloat(b.top3_rate) || 0) - (parseFloat(a.top3_rate) || 0);
            });
            for(let i=0; i<sorted.length; i++) {
                if ((parseFloat(sorted[i].win_rate)||0) === 0 && (parseFloat(sorted[i].top3_rate)||0) === 0) continue;
                if (i > 0 && sorted[i].win_rate === sorted[i-1].win_rate && sorted[i].top3_rate === sorted[i-1].top3_rate) {
                    rankMap.set(sorted[i].name, rankMap.get(sorted[i-1].name));
                } else {
                    rankMap.set(sorted[i].name, i + 1);
                }
            }
        }
        
        return arr.map(item => {
            let rowStyle = "";
            if (highlight) {
                let r = rankMap.get(item.name);
                if (r === 1) rowStyle = "color: #ff3366; font-weight: bold;";
                else if (r === 2) rowStyle = "color: #33cc66; font-weight: bold;";
                else if (r === 3) rowStyle = "color: #33ccff; font-weight: bold;";
            }
            return `<tr style="${rowStyle}">${cols.map(c => `<td>${item[c]}</td>`).join('')}</tr>`;
        }).join('');
    };
    
    document.getElementById(prefix+'UmabanTbody').innerHTML = buildTrs(stats.umaban, ['name', 'win_rate', 'top3_rate']);
    
    const wakuEl = document.getElementById(prefix+'WakuTbody');
    if (wakuEl && stats.waku) wakuEl.innerHTML = buildTrs(stats.waku, ['name', 'win_rate', 'top3_rate'], true);

    document.getElementById(prefix+'KyakuTbody').innerHTML = buildTrs(stats.kyakushitsu, ['name', 'win_rate', 'top3_rate']);
    document.getElementById(prefix+'JockeyTbody').innerHTML = buildTrs(stats.jockey, ['name', 'win_rate', 'top3_rate']);
    
    const weightEl = document.getElementById(prefix+'WeightTbody');
    if (weightEl && stats.weight) weightEl.innerHTML = buildTrs(stats.weight, ['name', 'win_rate', 'top3_rate'], true);
}

async function fetchTrackBias(venueOverride) {
    // venueOverride あり = 自動切替（キャッシュ優先・アラート非表示）
    // venueOverride なし = ボタン手動クリック
    const isAuto = !!venueOverride;

    let place = venueOverride || null;
    if (!place) {
        const rInfo = document.getElementById('raceInfo').textContent;
        const places = ["札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"];
        for (let p of places) { if (rInfo.includes(p)) place = p; }
    }

    if (!place) {
        if (!isAuto) alert("出馬表を先に解析し、競馬場を特定してください。");
        return;
    }

    const btn = document.getElementById('trackBiasBtn');
    btn.textContent = "解析中...";
    btn.disabled = true;

    try {
        // キャッシュ確認（手動クリックはキャッシュ破棄して再取得）
        if (!isAuto) delete trackBiasCache[place];
        if (trackBiasCache[place] !== undefined) {
            renderTrackBias(trackBiasCache[place]);
            return;
        }

        const response = await fetch(`api/track_bias?place=${encodeURIComponent(place)}`);
        const data = await response.json();
        trackBiasCache[place] = data;
        renderTrackBias(data);

    } catch (err) {
        if (!isAuto) alert("トラックバイアスの取得に失敗しました: " + err.message);
    } finally {
        btn.textContent = "🔍 直近実績（トラックバイアス）解析";
        btn.disabled = false;
    }
}

function renderTrackBias(data) {
    const container = document.getElementById('trackBiasContainer');

    // エラー or データなし
    if (data.error) {
        const noData = data.error.includes('No recent races') || data.error.includes('No recent');
        document.getElementById('tbPlace').textContent = noData ? '—' : '?';
        document.getElementById('tbDate').textContent  = noData ? '直近データなし' : 'エラー';
        document.getElementById('tbBody').innerHTML =
            `<div class="tb-no-data" style="grid-column:1/-1">${noData ? 'この競馬場の直近開催データがありません' : data.error}</div>`;
        container.style.display = 'block';
        return;
    }

    let formattedDate = data.latest_date || '';
    if (formattedDate.length === 6) {
        formattedDate = `20${formattedDate.slice(0,2)}年${formattedDate.slice(2,4)}月${formattedDate.slice(4,6)}日`;
    }
    document.getElementById('tbPlace').textContent = data.place || '—';
    document.getElementById('tbDate').textContent  = formattedDate;

    const speedCatClass = { fast:'fast', slightly_fast:'slightly_fast', slow:'slow', slightly_slow:'slightly_slow', normal:'flat' };
    const tracks = [
        { key: '芝',    label: '🌿 芝レース',    titleColor: '#4ade80' },
        { key: 'ダート', label: '🟤 ダートレース', titleColor: '#facc15' },
    ];

    document.getElementById('tbBody').innerHTML = tracks.map(({ key, label, titleColor }) => {
        const ev = (data.evaluations || {})[key];
        const sp = (data.track_speed || {})[key];

        // 速度バッジHTML
        let speedHtml = '';
        if (sp) {
            const cls = speedCatClass[sp.category] || 'flat';
            const sign = sp.avg_diff >= 0 ? '+' : '';
            speedHtml = `<div class="tb-speed-row">
                <span class="tb-verdict ${cls}">${sp.label}</span>
                <span class="tb-speed-diff">${sign}${sp.avg_diff}秒（基準比 ${sp.samples}R平均）</span>
            </div>`;
        }

        if (!ev || ev.kyaku === 'データなし') {
            return `<div class="tb-panel">
                <div class="tb-panel-title" style="color:${titleColor}">${label}</div>
                ${speedHtml}
                <div class="tb-no-data">バイアスデータなし</div>
            </div>`;
        }

        const nigeScore  = ev.nige_score  || 0;
        const sashiScore = ev.sashi_score || 0;
        const inScore    = ev.in_score    || 0;
        const outScore   = ev.out_score   || 0;
        const kyakuTotal = nigeScore + sashiScore;
        const wakuTotal  = inScore + outScore;
        const nigePct  = kyakuTotal > 0 ? Math.round(nigeScore  / kyakuTotal * 100) : 50;
        const sashiPct = kyakuTotal > 0 ? Math.round(sashiScore / kyakuTotal * 100) : 50;
        const inPct    = wakuTotal  > 0 ? Math.round(inScore    / wakuTotal  * 100) : 50;
        const outPct   = wakuTotal  > 0 ? Math.round(outScore   / wakuTotal  * 100) : 50;
        const kyakuCls = ev.kyaku.includes('前') ? 'front' : ev.kyaku.includes('差') ? 'sashi' : 'flat';
        const wakuCls  = ev.waku.includes('イン') ? 'inner' : ev.waku.includes('外') ? 'outer' : 'flat';

        // 枠バイアス強度バッジHTML (T75, T75b: 直近複数開催日を近い日ほど重視して判定)
        let wakuBiasHtml = '';
        const wb = (data.waku_bias || {})[key];
        if (wb) {
            const wbCls = wb.level === '強' ? 'strong' : wb.level === '中' ? 'medium' : 'flat';
            const dirLevelText = wb.direction === 'なし'
                ? (wb.level === 'データ不足' ? 'データ不足' : '弱 (フラット)')
                : `${wb.direction}${wb.level}`;
            const wbDays = wb.days || [];
            const nDays = wbDays.length || 1;
            const fmtMD = (d) => (d && d.length === 6) ? `${parseInt(d.slice(2,4),10)}/${parseInt(d.slice(4,6),10)}` : (d || '?');
            const daysTitle = wbDays
                .map(d => `${fmtMD(d.date)} ×${d.weight}: 内${d.in_index}/外${d.out_index} (${d.races}R)`)
                .join('\n');
            wakuBiasHtml = `<div class="tb-waku-bias" ${daysTitle ? `title="${daysTitle.replace(/"/g, '&quot;')}"` : ''}>
                <span class="tb-verdict ${wbCls}">枠バイアス: ${dirLevelText}</span>
                <span class="tb-speed-diff">内 ${wb.in.index}倍 / 外 ${wb.out.index}倍 (3着内/期待, ${nDays}日 ${wb.races}R・近い日ほど重視)</span>
            </div>`;
        }

        return `<div class="tb-panel">
            <div class="tb-panel-title" style="color:${titleColor}">${label}</div>
            ${speedHtml}
            <div class="tb-chart-group">
                <div class="tb-chart-label">脚質傾向（加重スコア）</div>
                <div class="tb-chart-row">
                    <span class="tb-chart-name">逃げ・先行</span>
                    <div class="tb-bar-track"><div class="tb-bar-fill nige" style="width:${nigePct}%"></div></div>
                    <span class="tb-pct">${nigePct}%</span>
                </div>
                <div class="tb-chart-row">
                    <span class="tb-chart-name">差し・追込</span>
                    <div class="tb-bar-track"><div class="tb-bar-fill sashi" style="width:${sashiPct}%"></div></div>
                    <span class="tb-pct">${sashiPct}%</span>
                </div>
                <div>→ <span class="tb-verdict ${kyakuCls}">${ev.kyaku}</span></div>
            </div>
            <div class="tb-chart-group">
                <div class="tb-chart-label">枠番傾向（加重スコア）</div>
                <div class="tb-chart-row">
                    <span class="tb-chart-name">内枠（1〜4枠）</span>
                    <div class="tb-bar-track"><div class="tb-bar-fill inner" style="width:${inPct}%"></div></div>
                    <span class="tb-pct">${inPct}%</span>
                </div>
                <div class="tb-chart-row">
                    <span class="tb-chart-name">外枠（5〜8枠）</span>
                    <div class="tb-bar-track"><div class="tb-bar-fill outer" style="width:${outPct}%"></div></div>
                    <span class="tb-pct">${outPct}%</span>
                </div>
                <div>→ <span class="tb-verdict ${wakuCls}">${ev.waku}</span></div>
            </div>
            ${wakuBiasHtml}
        </div>`;
    }).join('');

    container.style.display = 'block';

    // 詳細ボタンを表示（race_details または result_table があるとき）
    const detailBtn = document.getElementById('tbDetailBtn');
    const rt = data.result_table || {};
    const hasResultTable = (rt['芝'] && rt['芝'].length > 0) || (rt['ダート'] && rt['ダート'].length > 0);
    if ((data.race_details && data.race_details.length > 0) || hasResultTable) {
        detailBtn.style.display = 'inline-block';
        detailBtn._biasData = data; // データを紐付け
    } else {
        detailBtn.style.display = 'none';
    }
    // 詳細パネルは閉じた状態にリセット
    document.getElementById('tbDetailContainer').style.display = 'none';
    detailBtn.textContent = '詳細表示 ▼';
}

// SPEC-T75c: 実データの結果表 (1〜3着の馬番・人気・枠・脚質、決着パターン) をsurfaceごとに描画
function buildResultTableHtml(data) {
    const resultTable = data.result_table || {};
    const tracks = [
        { key: '芝',    emoji: '🌿' },
        { key: 'ダート', emoji: '🟤' },
    ];

    const fmtMD = (d) => (d && d.length === 6) ? `${parseInt(d.slice(2, 4), 10)}/${parseInt(d.slice(4, 6), 10)}` : (d || '?');

    const sections = tracks.map(({ key, emoji }) => {
        const races = resultTable[key] || [];
        if (!races.length) return '';

        const windowDays = (data.waku_bias && data.waku_bias[key] && data.waku_bias[key].window_days) || 14;

        const cellFor = (entry, race) => {
            if (!entry) return '-';
            const groupCls = entry.group === '内' ? 'tbd-in' : entry.group === '外' ? 'tbd-out' : '';
            const popHtml = entry.pop != null
                ? (entry.pop <= 3 ? `<b>${entry.pop}人気</b>` : entry.pop >= 6 ? `<span class="tbd-pop-hi">${entry.pop}人気</span>` : `${entry.pop}人気`)
                : '?人気';
            const wakuLabel = entry.waku != null ? `${entry.waku}枠` : '?枠';
            const oddsLabel = (entry.rank === 1 && race.win_odds) ? ` <span class="tbd-w-sub">(${race.win_odds}円)</span>` : '';
            const nameHtml = entry.name ? ` <span class="tbd-hname">${entry.name}</span>` : '';
            return `<span class="${groupCls}">${entry.num != null ? entry.num + '番' : '?番'}${nameHtml} ${popHtml} ${wakuLabel} ${entry.kyaku || '?'}</span>${oddsLabel}`;
        };

        const rowsHtml = races.map(r => {
            const top3ByRank = {};
            (r.top3 || []).forEach(t => { if (!top3ByRank[t.rank]) top3ByRank[t.rank] = t; });

            const raceLabel = r.race_num != null ? `${r.race_num}R` : '?R';
            const nameLabel = r.distance != null ? `${r.race_name || ''}（${r.distance}m）` : (r.race_name || '');
            const fkCls = r.finish_kyaku === '前残り' ? 'tbd-front' : (r.finish_kyaku === '差し決着' ? 'tbd-closer' : '');
            const fwCls = (r.finish_waku === '内決着' || r.finish_waku === '内寄り') ? 'tbd-in'
                        : (r.finish_waku === '外決着' || r.finish_waku === '外寄り') ? 'tbd-out' : '';

            return `<tr>
                <td class="tbd-c">${fmtMD(r.date)}<span class="tbd-w-sub"> ×${r.weight}</span></td>
                <td class="tbd-r">${raceLabel}</td>
                <td class="tbd-name">${nameLabel}</td>
                <td class="tbd-c">${r.condition || '-'}</td>
                <td class="tbd-c">${cellFor(top3ByRank[1], r)}</td>
                <td class="tbd-c">${cellFor(top3ByRank[2], r)}</td>
                <td class="tbd-c">${cellFor(top3ByRank[3], r)}</td>
                <td class="tbd-c ${fkCls}">${r.finish_kyaku || '?'}</td>
                <td class="tbd-c ${fwCls}">${r.finish_waku || '?'}</td>
            </tr>`;
        }).join('');

        return `<div class="tbd-section">
            <div class="tbd-title">${emoji} ${key} — 直近${windowDays}日の結果（1〜3着）</div>
            <div style="overflow-x:auto">
            <table class="tbd-table">
                <thead>
                    <tr>
                        <th>日付</th><th>R</th><th>レース名（距離）</th><th>馬場</th>
                        <th>1着</th><th>2着</th><th>3着</th><th>脚質決着</th><th>枠決着</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
            </div>
        </div>`;
    }).join('');

    return sections;
}

// 従来の「上位3着以内馬の加重スコア内訳」テーブル (折りたたみ内に格納)
function buildWeightBreakdownTableHtml(data) {
    const details = (data.race_details || []);
    const tracks = ['芝', 'ダート'];

    const html = tracks.map(tt => {
        const rows = details.filter(d => d.track_type === tt);
        if (!rows.length) return '';

        const rowsHtml = rows.map(d => {
            const raceLabel = d.race_num != null ? `${d.race_num}R` : '?R';
            const nige  = d.nige_pt  > 0 ? `<span class="tb-score-pos">逃/先 +${d.nige_pt}</span>`  : '';
            const sashi = d.sashi_pt > 0 ? `<span class="tb-score-pos">差/追 +${d.sashi_pt}</span>` : '';
            const inn   = d.in_pt    > 0 ? `<span class="tb-score-pos">内枠 +${d.in_pt}</span>`    : '';
            const out   = d.out_pt   > 0 ? `<span class="tb-score-pos">外枠 +${d.out_pt}</span>`   : '';
            const scores = [nige, sashi, inn, out].filter(Boolean).join(' ');
            return `<tr>
                <td class="tbd-r">${raceLabel}</td>
                <td class="tbd-name">${d.race_name || ''}</td>
                <td class="tbd-c">${d.rank}着</td>
                <td class="tbd-c">${d.horse_num != null ? d.horse_num + '番' : '-'}</td>
                <td class="tbd-c">${d.waku != null ? d.waku + '枠' : '-'}</td>
                <td class="tbd-c">${d.popularity}人気</td>
                <td class="tbd-w">×${d.weight}</td>
                <td class="tbd-kyaku">${d.kyaku}</td>
                <td class="tbd-wlabel">${d.waku_label}</td>
                <td class="tbd-scores">${scores || '-'}</td>
            </tr>`;
        }).join('');

        return `<div class="tbd-section">
            <div class="tbd-title">${tt === '芝' ? '🌿 芝' : '🟤 ダート'} — 上位3着以内馬の加重スコア内訳</div>
            <table class="tbd-table">
                <thead>
                    <tr>
                        <th>レース</th><th>レース名</th><th>着順</th><th>馬番</th>
                        <th>枠番</th><th>人気</th><th>加重W</th><th>脚質</th><th>枠分類</th><th>加算スコア</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        </div>`;
    }).join('');

    return html || '<div class="tbd-empty">詳細データなし</div>';
}

function toggleBiasDetail() {
    const container = document.getElementById('tbDetailContainer');
    const btn       = document.getElementById('tbDetailBtn');
    const data      = btn._biasData;

    if (container.style.display !== 'none') {
        container.style.display = 'none';
        btn.textContent = '詳細表示 ▼';
        return;
    }

    const resultTableHtml = buildResultTableHtml(data);
    const weightBreakdownHtml = buildWeightBreakdownTableHtml(data);

    document.getElementById('tbDetailBody').innerHTML = `
        ${resultTableHtml}
        <div class="tbd-sub-toggle-wrap">
            <button type="button" class="tbd-sub-toggle" onclick="toggleWeightBreakdown()">加重スコア内訳を表示 ▼</button>
            <div id="tbdWeightBreakdown" style="display:none">${weightBreakdownHtml}</div>
        </div>
    `;
    container.style.display = 'block';
    btn.textContent = '詳細を閉じる ▲';
}

function toggleWeightBreakdown() {
    const el  = document.getElementById('tbdWeightBreakdown');
    const btn = el ? el.previousElementSibling : null;
    if (!el) return;
    const opening = el.style.display === 'none';
    el.style.display = opening ? 'block' : 'none';
    if (btn) btn.textContent = opening ? '加重スコア内訳を閉じる ▲' : '加重スコア内訳を表示 ▼';
}

// ---- T77 race pick pure functions (begin) ----
// SPEC-T77 §2.2-1: races (オッズ監視の状態 st.races 相当) から「次に発走する
// レース」を1件選ぶ純関数。url を持つ要素のみを対象に start_time (文字列
// "HH:MM") 昇順で安定ソートし、start_time >= nowHHMM の最初の要素を返す。
// 該当が無ければ (全レース発走済みなら) 先頭の要素を返す。対象が空なら null。
function pickNextRace(races, nowHHMM) {
    const withUrl = (races || []).filter(r => r && r.url);
    if (withUrl.length === 0) return null;
    const sorted = withUrl.slice().sort((a, b) => {
        const at = a.start_time || '';
        const bt = b.start_time || '';
        if (at < bt) return -1;
        if (at > bt) return 1;
        return 0;
    });
    const next = sorted.find(r => (r.start_time || '') >= nowHHMM);
    return next || sorted[0];
}
// ---- T77 race pick pure functions (end) ----

window.JRA_RACE_PICK = { pickNextRace };

