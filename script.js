let globalHorsesData = [];
let raceCache = {}; // ◎がいるレースのハッシュマップ
let apiCache = {}; // { URL: { mode: "詳細", data: {...} } }
let currentRaceContext = { venue: "", track_type: "", distance: 0, condition: "", race_class: "" };

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
    document.getElementById('getUrlBtn').addEventListener('click', async () => {
        const btn = document.getElementById('getUrlBtn');
        btn.disabled = true;
        btn.textContent = "取得中...";
        try {
            const day = new Date().getDay();
            const response = await fetch(`/api/latest_url?day=${day}`);
            const data = await response.json();
            if (data.error) throw new Error(data.error);
            if (data.url) {
                document.getElementById('urlInput').value = data.url;
                alert("最新のURLを取得しました。\n" + data.url);
            }
        } catch(err) {
            alert("URL取得エラー: " + err.message);
        } finally {
            btn.disabled = false;
            btn.textContent = "最新URL取得";
        }
    });

    document.getElementById('historyHorseSelect').addEventListener('change', updateHistoryTable);
    document.getElementById('runAiBtn').addEventListener('click', runAiPrediction);
    
    // Past Data Analysis
    const pastDataBtn = document.getElementById('runPastDataBtn');
    if(pastDataBtn) pastDataBtn.addEventListener('click', fetchPastData);
    
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
    renderMatrix(data.matrix_data, data.venue);
    
    let ultraText = "";
    globalHorsesData.forEach(h => {
        if(h.ultra_details && h.ultra_details.length > 0) {
            ultraText += `【${h.num}番 ${h.name}】\n`;
            h.ultra_details.forEach(d => { ultraText += `    ∟ ${d}\n` });
            ultraText += `\n`;
        }
    });
    document.getElementById('ultraDetails').textContent = ultraText || "ウルトラ判定に該当する馬はいません。";
    
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
    
    // Fetch Wind Data
    fetchWindData(data.venue);
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
            h.num, h.grade || '-', h.odds, h.pop || '-', h.iv,
            h.name, h.sex_age, h.kyakushitsu, h.kg, h.jock || h.jockey,
            h.affi, h.sire, h.bms
        ];
        
        tr.appendChild(tdWaku);
        cols.forEach((val, idx) => {
            const td = document.createElement('td');
            td.textContent = val;
            
            if (idx === 1 && val === '◎') {
                td.classList.add('grade-tooltip-target');
                const tooltipSpan = document.createElement('span');
                tooltipSpan.className = 'tooltip-text';
                tooltipSpan.innerHTML = h.ultra_details && h.ultra_details.length > 0 
                    ? h.ultra_details.join('<br>') 
                    : '詳細データなし';
                td.appendChild(tooltipSpan);
            }
            
            if(val === h.name) td.style.textAlign = 'left';
            tr.appendChild(td);
        });
        
        tbody.appendChild(tr);
    });
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

    const buildTrs = (arr, cols) => (arr && arr.length > 0) 
        ? arr.map(item => `<tr>${cols.map(c => `<td>${item[c]}</td>`).join('')}</tr>`).join('')
        : '<tr><td colspan="3">データなし</td></tr>';
    
    document.getElementById(prefix+'UmabanTbody').innerHTML = buildTrs(stats.umaban, ['name', 'win_rate', 'top3_rate']);
    
    const wakuEl = document.getElementById(prefix+'WakuTbody');
    if (wakuEl && stats.waku) wakuEl.innerHTML = buildTrs(stats.waku, ['name', 'win_rate', 'top3_rate']);

    document.getElementById(prefix+'KyakuTbody').innerHTML = buildTrs(stats.kyakushitsu, ['name', 'win_rate', 'top3_rate']);
    document.getElementById(prefix+'JockeyTbody').innerHTML = buildTrs(stats.jockey, ['name', 'win_rate', 'top3_rate']);
    
    const weightEl = document.getElementById(prefix+'WeightTbody');
    if (weightEl && stats.weight) weightEl.innerHTML = buildTrs(stats.weight, ['name', 'win_rate', 'top3_rate']);
}
