let globalHorsesData = [];
let raceCache = {}; // ◎がいるレースのハッシュマップ
let apiCache = {}; // { URL: { mode: "詳細", data: {...} } }



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
    document.getElementById('historyHorseSelect').addEventListener('change', updateHistoryTable);
    document.getElementById('runAiBtn').addEventListener('click', runAiPrediction);
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
    document.getElementById('babaInfo').textContent = data.baba_info || "馬場情報：未取得";
    
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
                if (raceCache[raceItem.url]) {
                    btn.classList.add('has-star');
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
