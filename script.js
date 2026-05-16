let globalHorsesData = [];
let raceCache = {}; // ◎がいるレースのハッシュマップ
let apiCache = {}; // { URL: { mode: "詳細", data: {...} } }
let currentRaceContext = { venue: "", track_type: "", distance: 0, condition: "", race_class: "" };
let trackBiasCache = {}; // { 競馬場名: APIレスポンス }
let globalMatrixData = null; // マトリクス表示用データ

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
            const response = await fetch(`/api/latest_url?day=${day}`);
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
            document.getElementById('urlInput').value = "";
            if (isManual) alert("URL取得エラー: " + err.message + "\n現在出馬表データはありません。");
        } finally {
            btn.disabled = false;
            btn.textContent = "最新URL取得";
        }
    }

    document.getElementById('getUrlBtn').addEventListener('click', () => autoFetchUrl(true));
    
    // Auto-fetch on page load
    autoFetchUrl(false);

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

async function startScraping() {
    const url = document.getElementById('urlInput').value.trim();
    const mode = document.getElementById('modeSelect').value;
    
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
        const response = await fetch('/api/scrape', {
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
    document.getElementById('raceInfo').textContent = `${data.race_info} (${mode})`;
    
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
        courseImage.onerror = () => { courseImage.style.display = 'none'; };
        courseImage.onload = () => { courseImage.style.display = 'block'; };
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
    
    // Reset past data state if any
    const pmContainer = document.getElementById('pastDataResultsContainer');
    if(pmContainer) pmContainer.style.display = 'none';
    const pmStatus = document.getElementById('pastDataStatus');
    if(pmStatus) pmStatus.textContent = '';
    
    // Notable Sires Rendering (NEW)
    window.lastRaceData = data;
    renderNotableSiresTable(data.notable_sires, `${data.venue} ${data.race_type}${data.dist_val}m`);

    // Fetch Wind Data
    fetchWindData(data.venue);
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

function getWindDirectionString(deg) {
    const directions = ["北", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東", "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"];
    return directions[Math.round(deg / 22.5)];
}

function getWindSpeedTerm(speed) {
    if (speed < 0.3) return "静穏";
    if (speed < 10) return "微風";
    if (speed < 15) return "やや強い風";
    if (speed < 20) return "強い風";
    if (speed < 30) return "非常に強い風";
    return "猛烈な風";
}

function checkWindEffectHtml(windDir, courseDir) {
    let diff = Math.abs(windDir - courseDir);
    if (diff > 180) diff = 360 - diff;
    
    let straightWind = "";
    let backstretchWind = "";
    let effect = "";
    
    if (diff <= 45) {
        straightWind = "向かい風";
        backstretchWind = "追い風";
        effect = "直線が向かい風となるため、逃げ・先行馬が有利になる傾向があります。";
    } else if (diff >= 135) {
        straightWind = "追い風";
        backstretchWind = "向かい風";
        effect = "直線が追い風となるため、差し・追込馬が有利になる傾向があります。";
    } else {
        straightWind = "横風";
        backstretchWind = "横風";
        effect = "直線は横風となるため、内外で影響が変わる可能性があります。";
    }
    
    return `向こう正面は${backstretchWind}、直線は${straightWind}となります。<br><span style="color:#ffcc00; font-weight:bold;">${effect}</span>`;
}

async function fetchWindData(venue) {
    const windDisplay = document.getElementById('windDataDisplay');
    if (!windDisplay) return;
    
    const course = COURSE_DIRECTION[venue];
    if (!course) {
        windDisplay.textContent = `風データ: ${venue}の緯度経度情報がありません`;
        return;
    }
    
    windDisplay.textContent = "風データを取得中...";
    try {
        const res = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${course.lat}&longitude=${course.lon}&current_weather=true`);
        const result = await res.json();
        if (result.current_weather) {
            const w = result.current_weather;
            const dirStr = getWindDirectionString(w.winddirection);
            const speedTerm = getWindSpeedTerm(w.windspeed);
            const effectHtml = checkWindEffectHtml(w.winddirection, course.dir);
            
            windDisplay.innerHTML = `<strong>リアルタイム風力データ (${venue}):</strong> ${dirStr}からの風 (${w.winddirection}°), ${speedTerm} (${w.windspeed}m/s)<br>${effectHtml}`;
        }
    } catch(e) {
        windDisplay.textContent = "風データの取得に失敗しました。";
    }
}

function renderHorsesTable(horses) {
    const tbody = document.getElementById('horsesTbody');
    tbody.innerHTML = '';
    
    horses.forEach(h => {
        const tr = document.createElement('tr');
        
        const tdWaku = document.createElement('td');
        tdWaku.textContent = h.waku || h.w_num;
        tdWaku.className = `waku-${h.waku || h.w_num}`;
        
        // Other Cols
        const cols = [
            h.num, h.grade || '-', h.odds, h.pop || '-', h.iv, h.dist_diff || '-',
            h.name, h.sex_age, h.kyakushitsu, h.kg, h.jock || h.jockey,
            h.affi, h.sire, h.bms
        ];
        
        tr.appendChild(tdWaku);
        cols.forEach((val, idx) => {
            const td = document.createElement('td');
            
            if (idx === 6) { // idx 6 is now h.name
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
                        histTd.colSpan = 15; 
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
            
            // Sire Ranking Highlight (idx 12 corresponds to h.sire now)
            if (idx === 12 && h.sire_rank) {
                if (h.sire_rank === 1) td.classList.add('sire-rank-1');
                else if (h.sire_rank <= 5) td.classList.add('sire-rank-2-5');
                else if (h.sire_rank <= 10) td.classList.add('sire-rank-6-10');
            }

            tr.appendChild(td);
        });
        
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
        const res = await fetch('/api/ai_predict', {
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

// ═══════════════════════════════════════════
// WIN5 予想機能
// ═══════════════════════════════════════════

let win5Data = null;

/**
 * マトリクスデータから WIN5 対象レースの accessD URL を探す。
 * マトリクスの text 例: "5/9(土) 2回東京5日" → 会場名と日付で照合。
 * さらに URL の CNAME に埋め込まれた日付も検証して翌日レースURLの混入を防ぐ。
 */
function findUrlFromMatrix(venue, raceNum, win5DateStr) {
    if (!globalMatrixData) return '';
    const dm = win5DateStr.match(/(\d+)月(\d+)日/);
    if (!dm) return '';
    const dateShort = `${parseInt(dm[1])}/${parseInt(dm[2])}`; // "5/9"

    // URL内のCNAME日付と比較するための "YYYYMMDD" 文字列を生成
    const year = new Date().getFullYear();
    const expectedDate = `${year}${String(parseInt(dm[1])).padStart(2,'0')}${String(parseInt(dm[2])).padStart(2,'0')}`; // "20260509"

    for (const venueData of globalMatrixData) {
        const text = venueData.text || '';
        // ラベルの日付・会場名で一次絞り込み
        if (!text.includes(venue)) continue;
        if (!text.startsWith(dateShort)) continue;
        const race = (venueData.races || []).find(r => r.r === raceNum);
        if (!race || !race.url) continue;
        // URLのCNAMEに含まれる日付を二重チェック（同一ページに翌日リンクが混在する場合の対策）
        const urlDateMatch = race.url.match(/(\d{8})\/[0-9A-Fa-f]+/);
        if (urlDateMatch && urlDateMatch[1] !== expectedDate) continue;
        return race.url;
    }
    return '';
}

async function loadWin5Races() {
    const btn = document.getElementById('win5LoadBtn');
    const status = document.getElementById('win5LoadStatus');
    btn.disabled = true;
    status.textContent = '取得中...';

    try {
        const res = await fetch('/api/win5_races');
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || '取得失敗');

        // マトリクスで解析済みのレースがあればURLを自動補完
        for (const race of data.races) {
            if (!race.url) {
                const matrixUrl = findUrlFromMatrix(race.venue, race.race_num, data.date);
                if (matrixUrl) race.url = matrixUrl;
            }
        }
        win5Data = data;
        document.getElementById('win5DateLabel').textContent = `📅 ${data.date} WIN5対象レース`;
        renderWin5Grid(data.races);
        document.getElementById('win5RaceSection').style.display = 'block';
        document.getElementById('win5Result').style.display = 'none';
        status.textContent = '取得完了';
    } catch(e) {
        status.textContent = 'エラー: ' + e.message;
    } finally {
        btn.disabled = false;
    }
}

function renderWin5Grid(races) {
    const grid = document.getElementById('win5RaceGrid');
    grid.innerHTML = races.map((race, i) => {
        const hasUrl = race.url ? '✅' : '⚠️';
        const urlInput = race.url ? '' :
            `<input id="win5url${i}" class="win5-url-input"
                    placeholder="accessD URLを貼り付け（例: https://www.jra.go.jp/JRADB/accessD.html?CNAME=...）">`;
        return `
        <div class="win5-race-card">
          <div class="win5-race-no">第${i + 1}レース</div>
          <div class="win5-race-venue">${race.venue} ${race.race_num}R</div>
          <div class="win5-race-time">${race.time} 発走</div>
          <div class="win5-url-status">${hasUrl} ${race.url ? 'URL取得済み' : 'URL手動入力'}</div>
          ${urlInput}
        </div>`;
    }).join('');
}

async function runWin5Prediction() {
    if (!win5Data) { alert('先にWIN5レースを取得してください'); return; }

    const pwd = document.getElementById('win5Password').value;
    if (!pwd) { alert('パスワードを入力してください'); return; }

    const ptRadio = document.querySelector('input[name="win5pts"]:checked');
    const pointLimit = ptRadio ? parseInt(ptRadio.value) : 100;

    // URL収集（自動取得 or 手動入力）
    const raceUrls = win5Data.races.map((race, i) => {
        if (race.url) return race.url;
        const inp = document.getElementById(`win5url${i}`);
        return inp ? inp.value.trim() : '';
    });

    const btn = document.getElementById('win5PredictBtn');
    const status = document.getElementById('win5PredictStatus');
    const resultEl = document.getElementById('win5Result');

    btn.disabled = true;
    status.textContent = '各レースのデータを取得中... (しばらくお待ちください)';
    resultEl.style.display = 'none';

    try {
        const res = await fetch('/api/win5_predict', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                race_urls: raceUrls,
                races_info: win5Data.races,
                point_limit: pointLimit,
                date: win5Data.date,
                password: pwd,
            }),
        });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || 'AI予想失敗');

        resultEl.textContent = data.result;
        resultEl.style.display = 'block';
        status.textContent = '完了しました';
    } catch(e) {
        resultEl.textContent = 'エラーが発生しました:\n' + e.message;
        resultEl.style.display = 'block';
        status.textContent = 'エラー';
    } finally {
        btn.disabled = false;
    }
}

function showLoading(text) {
    document.getElementById('loadingOverlay').classList.remove('hidden');
    if(text) document.getElementById('loadingText').textContent = text;
}
function hideLoading() {
    document.getElementById('loadingOverlay').classList.add('hidden');
}

async function fetchPastData() {
    if(!currentRaceContext.venue) {
        alert("レースデータがありません。先に解析を実行してください。");
        return;
    }
    const btn = document.getElementById('runPastDataBtn');
    const status = document.getElementById('pastDataStatus');
    btn.disabled = true;
    status.textContent = "データ集計中...";
    document.getElementById('pastDataResultsContainer').style.display = 'none';

    try {
        const matchClass = document.getElementById('matchClassCheckbox').checked;
        const matchCond = document.getElementById('matchConditionCheckbox').checked;

        let url = `/api/past_data?place=${encodeURIComponent(currentRaceContext.venue)}&track_type=${encodeURIComponent(currentRaceContext.track_type)}&distance=${currentRaceContext.distance}`;
        
        if (matchCond && currentRaceContext.condition) {
            url += `&condition=${encodeURIComponent(currentRaceContext.condition)}`;
        }
        if (matchClass && currentRaceContext.race_class) {
            url += `&race_class=${encodeURIComponent(currentRaceContext.race_class)}`;
        }
        
        const res = await fetch(url);
        const data = await res.json();
        if(data.error) throw new Error(data.error);

        const condLabel = matchCond ? `[${currentRaceContext.condition}]` : "[馬場状態: 不問]";
        const classLabel = matchClass ? ` [${currentRaceContext.race_class}]` : " [クラス: 不問]";

        document.getElementById('matchStatusLabel').textContent = `${currentRaceContext.venue} ${currentRaceContext.track_type} ${currentRaceContext.distance}m ${condLabel}${classLabel}`;
        
        renderPastDataStats(data.results, 'res');

        document.getElementById('pastDataResultsContainer').style.display = 'flex';
        status.textContent = "集計完了";
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

        const response = await fetch(`/api/track_bias?place=${encodeURIComponent(place)}`);
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
        </div>`;
    }).join('');

    container.style.display = 'block';

    // 詳細ボタンを表示（race_details があるとき）
    const detailBtn = document.getElementById('tbDetailBtn');
    if (data.race_details && data.race_details.length > 0) {
        detailBtn.style.display = 'inline-block';
        detailBtn._biasData = data; // データを紐付け
    } else {
        detailBtn.style.display = 'none';
    }
    // 詳細パネルは閉じた状態にリセット
    document.getElementById('tbDetailContainer').style.display = 'none';
    detailBtn.textContent = '詳細表示 ▼';
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

    // テーブル生成
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

    document.getElementById('tbDetailBody').innerHTML = html || '<div class="tbd-empty">詳細データなし</div>';
    container.style.display = 'block';
    btn.textContent = '詳細を閉じる ▲';
}

