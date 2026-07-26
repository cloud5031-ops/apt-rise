let aptData = null;
let selectedArea = null;
let chartJsLoaded = false;
let scatterChartInst = null;
let monthlyChartInst = null;
let mapLoaded = false;
let mapLoading = false;
let mapInst = null;

const urlParams = new URLSearchParams(window.location.search);
const aptKey = urlParams.get('key');

function goBack() {
  window.location.href = 'index.html';
}

function parseToValidDate(dStr) {
  if (!dStr) return null;
  let s = String(dStr).trim();
  if (s.length === 8 && !s.includes('-') && !s.includes('.')) {
    s = s.substring(0, 4) + '-' + s.substring(4, 6) + '-' + s.substring(6, 8);
  } else if (s.includes('.')) {
    s = s.replace(/\./g, '-');
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return null;
  return d;
}

function formatDate(dStr) {
  const d = parseToValidDate(dStr);
  if (!d) return String(dStr);
  return `${d.getFullYear()}.${(d.getMonth() + 1).toString().padStart(2, '0')}.${d.getDate().toString().padStart(2, '0')}`;
}

function getPriceWon(t) {
  if (t.priceWon !== undefined) return Number(t.priceWon);
  if (t.price !== undefined) return Number(t.price);
  return 0;
}

function formatPriceWon(value) {
  const won = Number(value);
  if (!Number.isFinite(won) || won <= 0) {
    return "-";
  }
  const eok = Math.floor(won / 100000000);
  const manwon = Math.round((won % 100000000) / 10000);
  
  if (eok > 0 && manwon > 0) {
    return `${eok}억 ${manwon.toLocaleString("ko-KR")}만원`;
  }
  if (eok > 0) {
    return `${eok}억원`;
  }
  return `${manwon.toLocaleString("ko-KR")}만원`;
}

function hideLoading() {
  const loader = document.getElementById('loading');
  if (loader) loader.style.display = 'none';
}

function renderFatalError(msg) {
  const ev = document.getElementById('error-view');
  if (!ev) return;
  ev.style.display = 'block';
  ev.innerHTML = `<h3>안내</h3><p>${msg}</p><button onclick="goBack()" style="margin-top:16px; padding:8px 16px; border:none; background:var(--primary); color:#fff; border-radius:4px; cursor:pointer;">목록으로 돌아가기</button>`;
}

async function init() {
  try {
    if (!aptKey) {
      throw new Error("아파트 식별자가 전달되지 않았습니다.");
    }
    
    const sggCode = aptKey.split('-')[0];
    if (!sggCode || sggCode === aptKey) {
      throw new Error("잘못된 단지 주소입니다.");
    }
    
    const detailUrl = `/data/details/${encodeURIComponent(sggCode)}/${encodeURIComponent(aptKey)}.json`;
    const response = await fetch(`${detailUrl}?v=${Date.now()}`, { cache: 'no-store' });
    
    if (!response.ok) {
      throw new Error(`detail_fetch_failed: ${response.status} ${detailUrl}`);
    }
    
    const contentType = response.headers.get("content-type");
    if (contentType && contentType.indexOf("application/json") === -1) {
      throw new Error("detail_fetch_failed: Not JSON");
    }
    
    aptData = await response.json();
    
    if (!aptData || !aptData.apartmentKey) {
       throw new Error("detail_fetch_failed: Invalid JSON schema");
    }
    
    if (!aptData.availableAreas || aptData.availableAreas.length === 0) {
      throw new Error("수집된 거래 면적 정보가 없습니다.");
    }
    
    const validAreas = aptData.availableAreas.filter(a => a !== null && a !== undefined && a !== '' && !isNaN(Number(a)) && Number(a) > 0);
    aptData.availableAreas = [...new Set(validAreas)].map(Number).sort((a,b) => a - b);
    
    if (aptData.availableAreas.length === 0) {
      throw new Error("유효한 면적 정보가 없습니다.");
    }
    
    selectedArea = aptData.availableAreas[0];
    
    renderHeader();
    renderAreaFilters();
    renderTrades();
    
    document.getElementById('content-view').style.display = 'block';
    
  } catch (error) {
    console.error('[detail_init_failed]', error);
    if (error.message.includes("detail_fetch_failed") || error.message.includes("단지 주소")) {
        renderFatalError("잘못된 단지 주소이거나 상세 데이터를 불러오지 못했습니다.");
    } else {
        renderFatalError(error.message);
    }
  } finally {
    hideLoading();
  }
}

function renderHeader() {
  document.getElementById('apt-name').innerText = aptData.apartmentName;
  const addr = `${aptData.sggCode} ${aptData.dongName}`; // fallback
  document.getElementById('apt-address').innerText = addr;
  const periods = aptData.referenceMonths || [];
  document.getElementById('apt-period').innerText = `수집 기준월: ${periods.join(', ')}`;
}

function renderAreaFilters() {
  const container = document.getElementById('area-filters');
  container.innerHTML = '';
  
  aptData.availableAreas.forEach(area => {
    const btn = document.createElement('button');
    btn.className = `chip ${selectedArea === area ? 'active' : ''}`;
    btn.innerText = `${area}㎡`;
    if (selectedArea === area) {
      btn.style.background = 'var(--primary)';
      btn.style.color = '#fff';
    }
    
    btn.onclick = () => {
      selectedArea = area;
      renderAreaFilters();
      renderTrades();
      if (document.getElementById('tab-trends').classList.contains('active')) {
           renderCharts();
      }
    };
    container.appendChild(btn);
  });
}

function renderTrades() {
  const tbody = document.getElementById('trades-body');
  tbody.innerHTML = '';
  
  const txs = Array.isArray(aptData.transactions) ? aptData.transactions : [];
  const validTrades = txs.filter(t => 
    t.exclusiveArea === selectedArea && t.cancellationStatus !== 'CANCELLED'
  );
  
  if (validTrades.length === 0) {
    document.getElementById('no-trades').style.display = 'block';
    document.querySelector('.trades-table').style.display = 'none';
    return;
  }
  
  document.getElementById('no-trades').style.display = 'none';
  document.querySelector('.trades-table').style.display = 'table';
  
  validTrades.forEach(t => {
    const tr = document.createElement('tr');
    const dealTypeBadge = t.dealType === '직거래' ? `<span class="direct-deal">직거래</span>` : '';
    
    tr.innerHTML = `
      <td>${formatDate(t.contractDate || t.dealDate)}</td>
      <td style="font-weight:600; color:var(--ink);">${formatPriceWon(getPriceWon(t))}${dealTypeBadge}</td>
      <td>${t.floor}층</td>
      <td>${t.exclusiveArea}㎡</td>
    `;
    tbody.appendChild(tr);
  });
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', (e) => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    
    e.target.classList.add('active');
    document.getElementById(e.target.dataset.target).classList.add('active');
    
    if (e.target.dataset.target === 'tab-trends') {
      renderCharts();
    } else if (e.target.dataset.target === 'tab-location') {
      loadKakaoMap();
      if (mapInst) {
         setTimeout(() => {
             mapInst.relayout();
             let l1 = null, l2 = null;
             if (aptData && aptData.location && Number.isFinite(Number(aptData.location.latitude))) {
                 l1 = Number(aptData.location.latitude); l2 = Number(aptData.location.longitude);
             } else if (aptData && Number.isFinite(Number(aptData.lat))) { 
                 l1 = Number(aptData.lat); l2 = Number(aptData.lng); 
             }
             if (l1 && l2) mapInst.setCenter(new window.kakao.maps.LatLng(l1, l2));
         }, 100);
      }
    }
  });
});

function loadChartJs() {
  return new Promise((resolve, reject) => {
    if (window.Chart) return resolve();
    const script = document.createElement('script');
    script.src = "https://cdn.jsdelivr.net/npm/chart.js";
    
    const timeout = setTimeout(() => reject(new Error('Chart.js load timeout')), 5000);
    
    script.onload = () => {
       clearTimeout(timeout);
       chartJsLoaded = true;
       resolve();
    };
    script.onerror = () => {
       clearTimeout(timeout);
       reject(new Error('Chart.js load failed'));
    };
    document.head.appendChild(script);
  });
}

async function renderCharts() {
  try {
    await loadChartJs();
  } catch (e) {
    document.getElementById('chart-error-scatter').style.display = 'block';
    document.getElementById('chart-error-monthly').style.display = 'block';
    document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');
    return;
  }
  
  document.getElementById('chart-error-scatter').style.display = 'none';
  document.getElementById('chart-error-monthly').style.display = 'none';
  document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'block');
  
  const txs = Array.isArray(aptData.transactions) ? aptData.transactions : [];
  const validTrades = txs.filter(t => 
    t.exclusiveArea === selectedArea && t.cancellationStatus !== 'CANCELLED'
  );
  
  if (scatterChartInst) scatterChartInst.destroy();
  if (monthlyChartInst) monthlyChartInst.destroy();
  
  if (validTrades.length === 0) {
     document.getElementById('chart-error-scatter').innerText = "해당 평형의 유효한 거래가 없어 그래프를 표시할 수 없습니다.";
     document.getElementById('chart-error-monthly').innerText = "해당 평형의 유효한 거래가 없어 그래프를 표시할 수 없습니다.";
     document.getElementById('chart-error-scatter').style.display = 'block';
     document.getElementById('chart-error-monthly').style.display = 'block';
     document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');
     return;
  }
  
  let minPrice = Infinity, maxPrice = -Infinity;
  const scatterData = [];
  const monthlyGroups = {};
  
  validTrades.forEach(t => {
    const p = getPriceWon(t);
    const d = parseToValidDate(t.contractDate || t.dealDate);
    
    if (!d || isNaN(p) || p <= 0 || t.cancellationStatus === 'CANCELLED') {
       return;
    }
    
    scatterData.push({ x: d.getTime(), y: p, raw: t });
    if (p < minPrice) minPrice = p;
    if (p > maxPrice) maxPrice = p;
    
    const mKey = `${d.getFullYear()}-${(d.getMonth()+1).toString().padStart(2,'0')}`;
    if(!monthlyGroups[mKey]) monthlyGroups[mKey] = [];
    monthlyGroups[mKey].push(p);
  });
  
  if (scatterData.length === 0) {
    document.getElementById('chart-container').innerHTML = '<div style="text-align:center; padding:40px; color:var(--sub);">선택한 평형의 유효한 거래 데이터가 없습니다.</div>';
    return;
  }
  
  const yMax = maxPrice * 1.05;
  const yMin = minPrice * 0.95;
  
  const agencyTrades = scatterData.filter(d => d.raw.dealType !== '직거래');
  const directTrades = scatterData.filter(d => d.raw.dealType === '직거래');
  
  const scatterCtx = document.getElementById('scatterChart').getContext('2d');
  scatterChartInst = new window.Chart(scatterCtx, {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: '중개거래',
          data: agencyTrades,
          backgroundColor: '#2563c9',
          pointRadius: 5,
          pointHoverRadius: 7
        },
        {
          label: '직거래',
          data: directTrades,
          backgroundColor: '#d6293e',
          pointStyle: 'rectRot',
          pointRadius: 6,
          pointHoverRadius: 8
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        tooltip: {
          callbacks: {
            label: function(ctx) {
              const t = ctx.raw.raw;
              return `${formatDate(t.contractDate || t.dealDate)} | ${formatPriceWon(getPriceWon(t))} | ${t.floor}층 | ${t.dealType}`;
            }
          }
        }
      },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            callback: function(val) {
              const d = new Date(val);
              // Scatter X-axis tick format (e.g. "04.10") to avoid overcrowding
              return `${(d.getMonth()+1).toString().padStart(2,'0')}.${d.getDate().toString().padStart(2,'0')}`;
            },
            maxTicksLimit: 6
          }
        },
        y: {
          max: yMax,
          min: yMin,
          ticks: {
            callback: function(val) {
              return formatPriceWon(val);
            }
          }
        }
      }
    }
  });
  
  const months = Object.keys(monthlyGroups).sort();
  const medians = [];
  const volumes = [];
  
  months.forEach(m => {
    const prices = monthlyGroups[m].sort((a,b) => a - b);
    let med = 0;
    const len = prices.length;
    if (len % 2 === 0) med = (prices[len/2 - 1] + prices[len/2]) / 2;
    else med = prices[Math.floor(len/2)];
    
    medians.push(med);
    volumes.push(len);
  });
  
  const monthlyCtx = document.getElementById('monthlyChart').getContext('2d');
  monthlyChartInst = new window.Chart(monthlyCtx, {
    type: 'line',
    data: {
      labels: months.map(m => m.replace('-', '.')),
      datasets: [
        {
          type: 'line',
          label: '실거래 중위가격',
          data: medians,
          borderColor: '#2563c9',
          backgroundColor: '#2563c9',
          yAxisID: 'y',
          tension: 0.1,
          pointRadius: 4
        },
        {
          type: 'bar',
          label: '거래 건수',
          data: volumes,
          backgroundColor: 'rgba(107, 118, 132, 0.3)',
          yAxisID: 'y1'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.datasetIndex === 0) {
                  return `실거래 중위가격: ${formatPriceWon(ctx.raw)}`;
              }
              return `거래 건수: ${ctx.raw}건`;
            },
            afterLabel: (ctx) => {
              if (ctx.datasetIndex === 0) {
                const idx = ctx.dataIndex;
                if (idx > 0) {
                  const prev = medians[idx-1];
                  const cur = medians[idx];
                  const rate = ((cur - prev) / prev * 100).toFixed(2);
                  if (volumes[idx-1] < 2 || volumes[idx] < 2) {
                    return `실거래 중위가격 변화율: 표본 부족`;
                  }
                  return `실거래 중위가격 변화율: ${rate > 0 ? '+' : ''}${rate}%`;
                }
                return '';
              }
            }
          }
        }
      },
      scales: {
        y: {
          type: 'linear',
          display: true,
          position: 'left',
          max: yMax,
          min: yMin,
          ticks: {
            callback: function(val) {
              return formatPriceWon(val);
            }
          }
        },
        y1: {
          type: 'linear',
          display: true,
          position: 'right',
          grid: { drawOnChartArea: false },
          min: 0,
          ticks: {
            stepSize: 1
          }
        }
      }
    }
  });
}

function showMapError(reason, msg) {
  console.error(`Map Load Failed: ${reason}`);
  const errEl = document.getElementById('map-error');
  const containerEl = document.getElementById('map-container');
  if (errEl) {
    errEl.innerText = msg;
    errEl.style.display = 'block';
  }
  if (containerEl) {
    containerEl.style.display = 'none';
  }
  mapLoaded = false;
  mapLoading = false;
}

function clearMapError() {
  const errEl = document.getElementById('map-error');
  const containerEl = document.getElementById('map-container');
  if (errEl) {
    errEl.innerText = '';
    errEl.style.display = 'none';
  }
  if (containerEl) {
    containerEl.style.display = 'block';
  }
}

function loadKakaoMap() {
  if (mapLoaded) return;
  
  clearMapError();
  
  if (!window.APP_CONFIG) {
     showMapError('config_missing', '지도 서비스를 불러오지 못했습니다.');
     return;
  }
  
  const key = window.APP_CONFIG.kakaoMapJavaScriptKey;
  if (!key || key.trim() === '' || key === '__KAKAO_JAVASCRIPT_KEY__') {
     showMapError('javascript_key_missing', '지도 서비스를 불러오지 못했습니다.');
     return;
  }

  let lat = null, lng = null;
  if (aptData && aptData.location && Number.isFinite(Number(aptData.location.latitude))) {
      lat = Number(aptData.location.latitude);
      lng = Number(aptData.location.longitude);
  } else if (aptData && Number.isFinite(Number(aptData.lat))) {
      lat = Number(aptData.lat);
      lng = Number(aptData.lng);
  }

  if (!lat || !lng) {
    showMapError('coordinates_missing', '지도에 표시할 단지 좌표 정보가 없습니다.');
    return;
  }
  
  function initMap() {
    try {
        clearMapError(); // Ensure container is block before init
        const container = document.getElementById('map-container');
        const pos = new window.kakao.maps.LatLng(lat, lng);
        const options = { center: pos, level: 3 };
        mapInst = new window.kakao.maps.Map(container, options);
        
        const marker = new window.kakao.maps.Marker({ position: pos });
        marker.setMap(mapInst);
        
        const overlayContent = `<div style="padding:4px 8px; background:var(--ink); color:#fff; border-radius:4px; font-size:0.75rem; font-weight:bold; white-space:nowrap; transform:translateY(-150%);">${aptData.apartmentName}</div>`;
        const customOverlay = new window.kakao.maps.CustomOverlay({
            position: pos,
            content: overlayContent,
            yAnchor: 1
        });
        customOverlay.setMap(mapInst);
        
        mapLoaded = true;
        mapLoading = false;
        
        setTimeout(() => {
            mapInst.relayout();
            mapInst.setCenter(pos);
        }, 100);
    } catch (e) {
        showMapError('map_init_failed', '지도 서비스를 불러오지 못했습니다.');
    }
  }

  if (window.kakao && window.kakao.maps && window.kakao.maps.load) {
    window.kakao.maps.load(() => {
      initMap();
    });
    return;
  }

  if (mapLoading) return;
  mapLoading = true;
  
  const existingScript = document.querySelector('script[src*="dapi.kakao.com/v2/maps/sdk.js"]');
  if (existingScript) {
      // If script exists but kakao.maps isn't ready yet, it's currently loading.
      // We will let the existing onload handle it.
      // However, to be robust, we'll just check again shortly.
      setTimeout(loadKakaoMap, 500);
      return;
  }

  const script = document.createElement('script');
  script.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(key)}&autoload=false`;
  
  const timeout = setTimeout(() => {
     showMapError('kakao_sdk_timeout', '지도 서비스를 불러오지 못했습니다.');
  }, 15000);
  
  script.onload = () => {
    clearTimeout(timeout);
    if (!window.kakao || !window.kakao.maps) {
      showMapError('kakao_namespace_missing', '지도 서비스를 불러오지 못했습니다.');
      return;
    }
    window.kakao.maps.load(() => {
      initMap();
    });
  };
  script.onerror = () => {
    clearTimeout(timeout);
    showMapError('kakao_sdk_network_error', '지도 서비스를 불러오지 못했습니다.');
  };
  document.head.appendChild(script);
}

window.onload = init;
