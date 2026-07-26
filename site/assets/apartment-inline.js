/* 순위 목록에서 아파트 행을 클릭하면 그 행 아래에 상세 패널을 펼친다.
 *
 * 기존 순위 렌더러(index.html의 renderApartmentRankingList 등)는 건드리지 않는다.
 * 행에는 data-apt-key 속성만 있으면 되고, 이 스크립트가 이벤트 위임으로 처리한다.
 * 목록이 다시 렌더되면(탭·필터·지역 변경) 패널 노드가 DOM에서 사라지므로
 * MutationObserver로 감지해 차트·지도를 정리한다.
 *
 * apartment.html / apartment.js 는 직접 URL 접근과 fallback 용으로 그대로 둔다.
 */
(function () {
  "use strict";

  var PYEONG_PER_M2 = 3.305785;
  var PAGE_SIZE = 10;
  var HISTORY_MONTHS = 36;

  // 상세 JSON 캐시. 값은 Promise이므로 같은 단지를 연타해도 fetch는 1회다.
  var apartmentDetailCache = new Map();

  var active = null; // { key, row, panelRow, data, selectedArea, tab, shown, charts, map }
  var panelSeq = 0;

  // ── 포매터 (apartment.js와 동일한 표시 규칙) ────────────────────

  function getPriceWon(t) {
    if (t.priceWon !== undefined) return Number(t.priceWon);
    if (t.price !== undefined) return Number(t.price);
    return 0;
  }

  function formatPriceWon(value) {
    var won = Number(value);
    if (!Number.isFinite(won) || won <= 0) return "-";
    var eok = Math.floor(won / 100000000);
    var manwon = Math.round((won % 100000000) / 10000);
    if (eok > 0 && manwon > 0) return eok + "억 " + manwon.toLocaleString("ko-KR") + "만원";
    if (eok > 0) return eok + "억원";
    return manwon.toLocaleString("ko-KR") + "만원";
  }

  function parseToValidDate(dStr) {
    if (!dStr) return null;
    var s = String(dStr).trim();
    if (s.length === 8 && s.indexOf("-") === -1 && s.indexOf(".") === -1) {
      s = s.substring(0, 4) + "-" + s.substring(4, 6) + "-" + s.substring(6, 8);
    } else if (s.indexOf(".") !== -1) {
      s = s.replace(/\./g, "-");
    }
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function formatDate(dStr) {
    var d = parseToValidDate(dStr);
    if (!d) return String(dStr);
    return d.getFullYear() + "." +
      String(d.getMonth() + 1).padStart(2, "0") + "." +
      String(d.getDate()).padStart(2, "0");
  }

  /* Y축 전용 짧은 가격 라벨. "7억", "7억 2천", "9천만" 형태로만 만든다.
   * 정확한 금액은 툴팁과 거래표(formatPriceWon)가 담당한다. */
  function formatAxisPrice(won) {
    var man = Math.round(Number(won) / 10000); // 만원 단위
    if (!Number.isFinite(man)) return "";
    if (man === 0) return "0";
    var eok = Math.floor(man / 10000);
    var rest = man % 10000;
    if (eok > 0) {
      if (rest === 0) return eok + "억";
      if (rest % 1000 === 0) return eok + "억 " + (rest / 1000) + "천";
      return eok + "억 " + rest.toLocaleString("ko-KR");
    }
    if (rest % 1000 === 0) return (rest / 1000) + "천만";
    return rest.toLocaleString("ko-KR") + "만";
  }

  // 사람이 읽기 좋은 눈금 간격(만원 단위 사다리)을 골라 축 경계를 맞춘다.
  var TICK_LADDER_MAN = [
    500, 1000, 2000, 2500, 5000,
    10000, 20000, 25000, 50000,
    100000, 200000, 250000, 500000, 1000000
  ];

  var MAX_PRICE_TICKS = 6;

  function niceAxis(minWon, maxWon, targetTicks) {
    var minMan = Number(minWon) / 10000;
    var maxMan = Number(maxWon) / 10000;
    if (!Number.isFinite(minMan) || !Number.isFinite(maxMan)) return null;
    if (maxMan === minMan) { minMan -= 1000; maxMan += 1000; }

    var raw = (maxMan - minMan) / (targetTicks || 5);
    var idx = TICK_LADDER_MAN.length - 1;
    for (var i = 0; i < TICK_LADDER_MAN.length; i++) {
      if (TICK_LADDER_MAN[i] >= raw) { idx = i; break; }
    }

    /* 최고/최저가가 축 경계에 딱 붙으면 점 반경만큼 잘리므로,
     * 경계와 데이터 사이에 최소 1/4 눈금의 headroom을 보장한다.
     * headroom 때문에 눈금이 6개를 넘으면 한 단계 굵은 눈금으로 다시 계산한다. */
    while (true) {
      var step = TICK_LADDER_MAN[idx];
      var lo = Math.floor(minMan / step) * step;
      var hi = Math.ceil(maxMan / step) * step;
      if (lo === hi) hi = lo + step;
      if (hi - maxMan < step * 0.25) hi += step;
      if (minMan - lo < step * 0.25) lo = Math.max(0, lo - step);
      var count = Math.round((hi - lo) / step) + 1;
      if (count <= MAX_PRICE_TICKS || idx >= TICK_LADDER_MAN.length - 1) {
        return { min: lo * 10000, max: hi * 10000, stepSize: step * 10000 };
      }
      idx++;
    }
  }

  // 표시 전용 면적 라벨. 반올림 없이 소수점만 버린다. 원본 값은 그대로 둔다.
  function areaLabel(areaM2) {
    var a = Number(areaM2);
    if (!Number.isFinite(a) || a <= 0) return "";
    return Math.trunc(a) + "㎡ (" + Math.trunc(a / PYEONG_PER_M2) + "평)";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ── 월 유틸 (36개월 X축용) ──────────────────────────────────────

  function shiftMonth(yyyymm, delta) {
    var y = parseInt(yyyymm.slice(0, 4), 10);
    var m = parseInt(yyyymm.slice(4, 6), 10);
    var total = y * 12 + (m - 1) + delta;
    return String(Math.floor(total / 12)).padStart(4, "0") +
      String((total % 12) + 1).padStart(2, "0");
  }

  function monthOf(t) {
    var d = String(t.contractDate || t.dealDate || "");
    if (d.length >= 7 && d[4] === "-") return d.slice(0, 4) + d.slice(5, 7);
    return "";
  }

  /* 최근 36개월 창을 확정한다.
   * 끝 기준월(anchor) 우선순위:
   *   1) historyCoverage.endMonth  — 백필/병합이 남긴 공식 anchor
   *   2) referenceMonths의 최대값  — 순위 산출에 쓰인 잠정 집계월
   *   3) 가장 최근 거래월          — 위 둘이 없을 때의 최후 수단
   * 거래 내역과 두 그래프가 모두 이 창 하나를 공유한다. */
  function historyWindow(data) {
    var hc = data.historyCoverage;
    var end = null;

    if (hc && hc.endMonth) {
      end = hc.endMonth;
    } else if (Array.isArray(data.referenceMonths) && data.referenceMonths.length) {
      end = data.referenceMonths.slice().sort()[data.referenceMonths.length - 1];
    } else {
      var ms = (data.transactions || []).map(monthOf).filter(Boolean).sort();
      end = ms.length ? ms[ms.length - 1] : null;
    }
    if (!end) return null;

    var start = (hc && hc.startMonth) ? hc.startMonth : shiftMonth(end, -(HISTORY_MONTHS - 1));
    var months = [];
    var m = start;
    // 거래가 없는 달도 X축 흐름을 유지한다
    for (var i = 0; i < HISTORY_MONTHS && m <= end; i++) {
      months.push(m);
      m = shiftMonth(m, 1);
    }
    return { start: start, end: end, months: months };
  }

  // ── 데이터 로딩 ────────────────────────────────────────────────

  function detailUrl(sggCode, key) {
    return "/data/details/" + encodeURIComponent(sggCode) + "/" + encodeURIComponent(key) + ".json";
  }

  function loadDetail(sggCode, key) {
    var cacheKey = sggCode + "/" + key;
    if (apartmentDetailCache.has(cacheKey)) return apartmentDetailCache.get(cacheKey);

    var p = fetch(detailUrl(sggCode, key), { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("detail_fetch_failed: " + res.status);
      var ct = res.headers.get("content-type");
      if (ct && ct.indexOf("application/json") === -1) throw new Error("detail_fetch_failed: not json");
      return res.json();
    }).then(function (data) {
      if (!data || !data.apartmentKey) throw new Error("detail_fetch_failed: invalid schema");
      var areas = (data.availableAreas || [])
        .map(Number).filter(function (a) { return Number.isFinite(a) && a > 0; });
      data.availableAreas = Array.from(new Set(areas)).sort(function (a, b) { return a - b; });
      data.transactions = Array.isArray(data.transactions) ? data.transactions : [];
      return data;
    }).catch(function (err) {
      // 실패한 Promise는 캐시에서 제거해 다음 클릭에 재시도할 수 있게 한다
      apartmentDetailCache.delete(cacheKey);
      throw err;
    });

    apartmentDetailCache.set(cacheKey, p);
    return p;
  }

  // ── 패널 정리 ──────────────────────────────────────────────────

  function destroyCharts() {
    if (!active || !active.charts) return;
    ["scatter", "monthly"].forEach(function (k) {
      if (active.charts[k]) {
        try { active.charts[k].destroy(); } catch (e) { /* noop */ }
        active.charts[k] = null;
      }
    });
  }

  function closePanel() {
    if (!active) return;
    destroyCharts();
    active.map = null;
    if (active.row && active.row.isConnected) {
      active.row.setAttribute("aria-expanded", "false");
      active.row.classList.remove("apt-row-open");
      active.row.removeAttribute("aria-controls");
    }
    if (active.panelRow && active.panelRow.parentNode) {
      active.panelRow.parentNode.removeChild(active.panelRow);
    }
    active = null;
  }

  // ── 렌더링 ─────────────────────────────────────────────────────

  /* 선택 평형의 유효 거래를 최근 36개월 창으로 제한해 최신순으로 돌려준다.
   * 면적 비교는 반드시 원본 exclusiveArea 값으로 한다 — 59.96과 59.97처럼
   * 화면에는 똑같이 "59㎡"로 보이는 평형이 합쳐지면 안 되기 때문이다. */
  function validTrades(data, area) {
    var win = active && active.window ? active.window : historyWindow(data);
    return data.transactions.filter(function (t) {
      if (t.cancellationStatus === "CANCELLED") return false;
      if (Number(t.exclusiveArea) !== Number(area)) return false;
      if (win) {
        var m = monthOf(t);
        if (!m || m < win.start || m > win.end) return false;
      }
      return true;
    }).sort(function (a, b) {
      return String(b.contractDate || "").localeCompare(String(a.contractDate || ""));
    });
  }

  // 36개월이 실제로 수집되지 않았으면 '최근 3년'이라고 말하지 않는다.
  function coverageNotice(data) {
    var hc = data.historyCoverage;
    var win = active && active.window ? active.window : historyWindow(data);
    var range = win ? (win.start.slice(0, 4) + "." + win.start.slice(4) + " ~ " +
      win.end.slice(0, 4) + "." + win.end.slice(4)) : "";
    if (hc && hc.complete === true) {
      return '<p class="apt-inline-note">최근 3년(36개월) 실거래 내역입니다. <span class="apt-inline-range">' +
        escapeHtml(range) + '</span></p>';
    }
    return '<p class="apt-inline-note">최근 3년(36개월) 기준으로 표시하며, 현재 수집이 완료된 기간의 거래만 담겨 있습니다.' +
      (range ? ' <span class="apt-inline-range">' + escapeHtml(range) + '</span>' : '') + '</p>';
  }

  function renderTrades(body) {
    var data = active.data;
    var trades = validTrades(data, active.selectedArea);
    if (!trades.length) {
      body.innerHTML = coverageNotice(data) +
        '<p class="apt-inline-empty">선택한 평형의 거래 내역이 없습니다.</p>';
      return;
    }
    var shown = Math.min(active.shown, trades.length);
    var rows = trades.slice(0, shown).map(function (t) {
      var direct = t.dealType === "직거래";
      return '<tr>' +
        '<td>' + escapeHtml(formatDate(t.contractDate)) + '</td>' +
        '<td class="apt-inline-price">' + escapeHtml(formatPriceWon(getPriceWon(t))) +
        (direct ? ' <span class="apt-inline-badge">직거래</span>' : '') + '</td>' +
        '<td>' + (t.floor != null ? escapeHtml(t.floor) + '층' : '-') + '</td>' +
        '<td>' + escapeHtml(areaLabel(t.exclusiveArea) || '-') + '</td>' +
        '<td>' + escapeHtml(t.dealType || '-') + '</td>' +
        '</tr>';
    }).join("");

    body.innerHTML = coverageNotice(data) +
      '<div class="apt-inline-scroll"><table class="apt-inline-table">' +
      '<thead><tr><th>계약일</th><th>거래가격</th><th>층</th><th>전용면적</th><th>거래유형</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      (shown < trades.length
        ? '<button type="button" class="apt-inline-more" data-inline-more="1">더보기 (' +
          shown + '/' + trades.length + ')</button>'
        : '<p class="apt-inline-count">전체 ' + trades.length + '건 표시</p>');
  }

  function loadChartJs() {
    return new Promise(function (resolve, reject) {
      if (window.Chart) return resolve();
      var script = document.querySelector('script[data-inline-chartjs]');
      if (script) {
        script.addEventListener("load", resolve);
        script.addEventListener("error", function () { reject(new Error("chartjs_failed")); });
        return;
      }
      script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/chart.js";
      script.setAttribute("data-inline-chartjs", "1");
      var timeout = setTimeout(function () { reject(new Error("chartjs_timeout")); }, 5000);
      script.onload = function () { clearTimeout(timeout); resolve(); };
      script.onerror = function () { clearTimeout(timeout); reject(new Error("chartjs_failed")); };
      document.head.appendChild(script);
    });
  }

  function renderTrends(body) {
    var data = active.data;
    var trades = validTrades(data, active.selectedArea).filter(function (t) {
      var p = getPriceWon(t);
      return parseToValidDate(t.contractDate) && Number.isFinite(p) && p > 0;
    });

    body.innerHTML = coverageNotice(data) +
      '<p class="apt-inline-note">공식 시세가 아니라 신고된 실거래 표본의 중위가격입니다.</p>' +
      '<div class="apt-inline-chart-msg" data-chart-msg hidden></div>' +
      '<div class="apt-inline-chart-block">' +
        '<h4 class="apt-inline-chart-title">실거래 가격</h4>' +
        '<div class="apt-inline-chart"><canvas data-chart="scatter"></canvas></div>' +
      '</div>' +
      '<div class="apt-inline-chart-block">' +
        '<h4 class="apt-inline-chart-title">월별 중위가격 · 거래 건수</h4>' +
        '<div class="apt-inline-chart monthly"><canvas data-chart="monthly"></canvas></div>' +
      '</div>';

    var msg = body.querySelector("[data-chart-msg]");
    function showMsg(text) {
      msg.textContent = text;
      msg.hidden = false;
      body.querySelectorAll(".apt-inline-chart").forEach(function (c) { c.style.display = "none"; });
    }

    if (!trades.length) {
      showMsg("해당 평형의 유효한 거래가 없어 그래프를 표시할 수 없습니다.");
      return;
    }

    var token = active.key + "|" + active.selectedArea;
    loadChartJs().then(function () {
      // 로딩 중에 패널이 닫히거나 평형이 바뀌었으면 그린 차트가 유령이 된다
      if (!active || active.key + "|" + active.selectedArea !== token) return;
      if (!body.isConnected) return;
      drawCharts(body, trades);
    }).catch(function () {
      if (!active || !body.isConnected) return;
      showMsg("그래프를 불러올 수 없습니다.");
    });
  }

  function drawCharts(body, trades) {
    destroyCharts();

    var win = active.window;
    var scatterData = [];
    var monthlyGroups = {};
    var minPrice = Infinity, maxPrice = -Infinity;

    trades.forEach(function (t) {
      var p = getPriceWon(t);
      var d = parseToValidDate(t.contractDate);
      scatterData.push({ x: d.getTime(), y: p, raw: t });
      if (p < minPrice) minPrice = p;
      if (p > maxPrice) maxPrice = p;
      var mk = monthOf(t);
      if (!monthlyGroups[mk]) monthlyGroups[mk] = [];
      monthlyGroups[mk].push(p);
    });

    // 두 그래프가 같은 가격 축을 쓰도록 눈금을 한 번만 계산한다
    var axis = niceAxis(minPrice, maxPrice, 5) || { min: undefined, max: undefined, stepSize: undefined };

    var months = (win && win.months.length) ? win.months : Object.keys(monthlyGroups).sort();
    var monthStartMs = months.length
      ? new Date(Number(months[0].slice(0, 4)), Number(months[0].slice(4)) - 1, 1).getTime()
      : undefined;
    var lastM = months[months.length - 1];
    var monthEndMs = months.length
      ? new Date(Number(lastM.slice(0, 4)), Number(lastM.slice(4)), 0, 23, 59, 59).getTime()
      : undefined;

    var gridColor = "rgba(0,0,0,0.06)";
    var legendCommon = {
      position: "top",
      align: "end",
      labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, padding: 12, font: { size: 11 } }
    };

    // ── 산점도: 36개월 프레임 위에 개별 거래를 찍는다 ──────────────
    var scatterCanvas = body.querySelector('canvas[data-chart="scatter"]');
    active.charts.scatter = new window.Chart(scatterCanvas.getContext("2d"), {
      type: "scatter",
      data: {
        datasets: [
          {
            label: "중개거래",
            data: scatterData.filter(function (d) { return d.raw.dealType !== "직거래"; }),
            backgroundColor: "rgba(37, 99, 201, 0.75)",
            borderColor: "#1b4b99",
            borderWidth: 1,
            pointRadius: 6,
            pointHoverRadius: 9,
            // 창 경계(마지막 달) 위의 점이 chartArea에서 잘리지 않게 한다.
            // layout.padding이 잉여 공간을 만들어 축 라벨 침범은 없다.
            clip: false
          },
          {
            label: "직거래",
            data: scatterData.filter(function (d) { return d.raw.dealType === "직거래"; }),
            backgroundColor: "rgba(240, 138, 20, 0.9)",
            borderColor: "#a85c00",
            borderWidth: 1,
            pointStyle: "triangle",
            pointRadius: 8,
            pointHoverRadius: 11,
            clip: false
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 16, right: 22, bottom: 8, left: 8 } },
        plugins: {
          legend: legendCommon,
          tooltip: {
            displayColors: false,
            callbacks: {
              title: function (items) { return formatDate(items[0].raw.raw.contractDate); },
              label: function (ctx) {
                var t = ctx.raw.raw;
                return [
                  formatPriceWon(getPriceWon(t)),
                  (t.floor != null ? t.floor + "층" : "") + " · " + (t.dealType || "")
                ];
              }
            }
          }
        },
        scales: {
          x: {
            type: "linear",
            min: monthStartMs,
            max: monthEndMs,
            grid: { color: gridColor, drawTicks: false },
            border: { color: gridColor },
            ticks: {
              // 36개월을 6칸 정도로 끊어 라벨이 겹치지 않게 한다
              maxTicksLimit: 6,
              autoSkip: true,
              maxRotation: 0,
              minRotation: 0,
              font: { size: 11 },
              callback: function (val) {
                var d = new Date(val);
                return String(d.getFullYear()).slice(2) + "." +
                  String(d.getMonth() + 1).padStart(2, "0");
              }
            }
          },
          y: {
            min: axis.min,
            max: axis.max,
            grid: { color: gridColor, drawTicks: false },
            border: { display: false },
            ticks: {
              stepSize: axis.stepSize,
              maxTicksLimit: MAX_PRICE_TICKS,
              font: { size: 11 },
              callback: function (v) { return formatAxisPrice(v); }
            }
          }
        }
      }
    });

    // ── 월별 중위가격(선) + 거래 건수(막대) ───────────────────────
    var medians = [];
    var volumes = [];
    months.forEach(function (m) {
      var prices = (monthlyGroups[m] || []).slice().sort(function (a, b) { return a - b; });
      if (!prices.length) { medians.push(null); volumes.push(0); return; }
      var len = prices.length;
      medians.push(len % 2 === 0 ? (prices[len / 2 - 1] + prices[len / 2]) / 2 : prices[Math.floor(len / 2)]);
      volumes.push(len);
    });

    // 막대가 가격선을 가리지 않도록 거래량 축 상한을 최대치의 3배로 잡아
    // 막대를 아래쪽 1/3 안에 가둔다
    var maxVol = Math.max.apply(null, volumes.concat([1]));
    var volAxisMax = Math.max(3, maxVol * 3);

    var monthlyCanvas = body.querySelector('canvas[data-chart="monthly"]');
    active.charts.monthly = new window.Chart(monthlyCanvas.getContext("2d"), {
      data: {
        labels: months.map(function (m) { return m.slice(0, 4) + "." + m.slice(4); }),
        datasets: [
          {
            type: "bar",
            label: "거래 건수",
            data: volumes,
            backgroundColor: "rgba(107, 118, 132, 0.25)",
            hoverBackgroundColor: "rgba(107, 118, 132, 0.45)",
            borderWidth: 0,
            yAxisID: "y1",
            order: 2,
            barPercentage: 0.9,
            categoryPercentage: 0.9
          },
          {
            type: "line",
            label: "중위가격",
            data: medians,
            borderColor: "#2563c9",
            backgroundColor: "#2563c9",
            borderWidth: 2,
            yAxisID: "y",
            tension: 0.25,
            pointRadius: 3,
            pointHoverRadius: 6,
            spanGaps: true,
            order: 1,
            clip: false
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { top: 16, right: 22, bottom: 8, left: 8 } },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: legendCommon,
          tooltip: {
            displayColors: true,
            callbacks: {
              label: function (ctx) {
                if (ctx.dataset.type === "bar") {
                  return "거래 건수: " + ctx.raw + "건";
                }
                if (ctx.raw == null) return "중위가격: 거래 없음";
                return "중위가격: " + formatPriceWon(ctx.raw);
              },
              afterBody: function (items) {
                var line = items.find(function (i) { return i.dataset.type === "line"; });
                if (!line) return "";
                var i = line.dataIndex;
                if (i <= 0 || medians[i] == null || medians[i - 1] == null) return "";
                if (volumes[i - 1] < 2 || volumes[i] < 2) return "전월 대비: 표본 부족";
                var rate = ((medians[i] - medians[i - 1]) / medians[i - 1] * 100).toFixed(1);
                return "전월 대비: " + (rate > 0 ? "+" : "") + rate + "%";
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: gridColor },
            ticks: {
              // 36칸을 전부 쓰지 않고 자동으로 솎아낸다
              maxTicksLimit: 7,
              autoSkip: true,
              maxRotation: 0,
              minRotation: 0,
              font: { size: 11 }
            }
          },
          y: {
            type: "linear",
            position: "left",
            min: axis.min,
            max: axis.max,
            grid: { color: gridColor, drawTicks: false },
            border: { display: false },
            ticks: {
              stepSize: axis.stepSize,
              maxTicksLimit: MAX_PRICE_TICKS,
              font: { size: 11 },
              callback: function (v) { return formatAxisPrice(v); }
            }
          },
          y1: {
            type: "linear",
            position: "right",
            min: 0,
            max: volAxisMax,
            grid: { drawOnChartArea: false },
            border: { display: false },
            ticks: {
              precision: 0,
              padding: 6,
              font: { size: 11 },
              // 막대 영역(아래 1/3) 밖의 눈금은 숨겨 축이 어수선해지지 않게 한다
              callback: function (v) { return v <= maxVol ? v + "건" : ""; }
            }
          }
        }
      }
    });
  }

  /* 운영 도메인 외의 Cloudflare Pages 주소(해시 프리뷰, *.pages.dev 서브도메인)에서는
   * 카카오 JS 키의 허용 도메인에 등록돼 있지 않아 SDK가 거부될 수 있다. */
  var PRODUCTION_HOSTS = ["apt-rise.pages.dev"];

  function isPreviewHost() {
    var h = location.hostname;
    if (PRODUCTION_HOSTS.indexOf(h) !== -1) return false;
    if (h === "localhost" || h === "127.0.0.1") return false;
    return /\.pages\.dev$/.test(h);
  }

  function renderLocation(body) {
    var data = active.data;
    var loc = data.location || {};
    var lat = Number(loc.latitude);
    var lng = Number(loc.longitude);

    body.innerHTML =
      '<p class="apt-inline-note">지도를 이동하거나 축소/확대할 수 있습니다.</p>' +
      '<div class="apt-inline-map-msg" data-map-msg hidden></div>' +
      '<div class="apt-inline-map" data-map></div>';

    var msgEl = body.querySelector("[data-map-msg]");
    var mapEl = body.querySelector("[data-map]");
    var settled = false;

    function fail(reason, text) {
      if (settled) return;
      settled = true;
      // console.error는 페이지 오류로 잡히므로, 예상 가능한 실패는 warn으로 남긴다
      console.warn("[inline_map] " + reason);
      msgEl.innerHTML = text;
      msgEl.hidden = false;
      mapEl.style.display = "none";   // 빈 회색 상자를 남기지 않는다
    }

    function succeeded() {
      settled = true;
      msgEl.hidden = true;
      mapEl.style.display = "";
    }

    // ① 좌표 자체가 없을 때만 '좌표 없음'을 말한다.
    //    geocodeStatus가 ambiguous여도 좌표가 유효하면 지도를 띄운다
    //    (기존 상세 페이지 apartment.js와 동일한 판정 기준).
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || (lat === 0 && lng === 0)) {
      fail("coordinates_missing", "지도에 표시할 단지 좌표 정보가 없습니다.");
      return;
    }

    // ② 키 설정 문제
    var key = window.APP_CONFIG && window.APP_CONFIG.kakaoMapJavaScriptKey;
    if (!key || !String(key).trim() || key === "__KAKAO_JAVASCRIPT_KEY__") {
      fail("javascript_key_missing", "지도 설정을 불러오지 못했습니다.");
      return;
    }

    var previewNotice =
      "Preview 도메인에서는 지도 SDK 도메인 제한으로 지도가 표시되지 않을 수 있습니다.<br>" +
      '운영 주소(<a href="https://apt-rise.pages.dev" target="_blank" rel="noopener">apt-rise.pages.dev</a>)에서는 정상 표시됩니다.';

    function sdkFailureText() {
      return isPreviewHost() ? previewNotice : "지도 서비스를 불러오지 못했습니다.";
    }

    var token = active.key;
    function stillCurrent() {
      return active && active.key === token && mapEl.isConnected;
    }

    function initMap() {
      if (!stillCurrent()) return;
      try {
        var pos = new window.kakao.maps.LatLng(lat, lng);
        var map = new window.kakao.maps.Map(mapEl, { center: pos, level: 3 });
        active.map = map;

        new window.kakao.maps.Marker({ position: pos }).setMap(map);
        new window.kakao.maps.CustomOverlay({
          position: pos, yAnchor: 1,
          content: '<div class="apt-inline-map-label">' + escapeHtml(data.apartmentName) + '</div>'
        }).setMap(map);

        succeeded();

        // 패널이 방금 표시됐으므로 크기를 다시 잡아준다
        setTimeout(function () {
          if (!active || active.map !== map) return;
          map.relayout();
          map.setCenter(pos);
        }, 100);

        // 도메인이 거부되면 SDK는 로드되지만 타일이 그려지지 않는다.
        // 컨테이너가 비어 있는지로 사후 확인한다.
        setTimeout(function () {
          if (!active || active.map !== map || !mapEl.isConnected) return;
          if (mapEl.childElementCount === 0 || mapEl.clientHeight === 0) {
            settled = false;
            fail("map_not_rendered", sdkFailureText());
          }
        }, 2500);
      } catch (e) {
        fail("map_init_failed", sdkFailureText());
      }
    }

    // ③ 이미 SDK가 준비된 경우
    if (window.kakao && window.kakao.maps && window.kakao.maps.load) {
      window.kakao.maps.load(initMap);
      return;
    }

    // ④ 다른 패널이 이미 SDK를 받아오는 중인 경우
    var existing = document.querySelector("script[data-inline-kakao]");
    if (existing) {
      existing.addEventListener("load", function () {
        if (window.kakao && window.kakao.maps) window.kakao.maps.load(initMap);
        else fail("kakao_namespace_missing", sdkFailureText());
      });
      existing.addEventListener("error", function () {
        fail("kakao_sdk_network_error", sdkFailureText());
      });
      return;
    }

    // ⑤ SDK 최초 로드
    var script = document.createElement("script");
    script.src = "https://dapi.kakao.com/v2/maps/sdk.js?appkey=" +
      encodeURIComponent(key) + "&autoload=false";
    script.setAttribute("data-inline-kakao", "1");
    var timeout = setTimeout(function () {
      fail("kakao_sdk_timeout", sdkFailureText());
    }, 10000);
    script.onload = function () {
      clearTimeout(timeout);
      if (!window.kakao || !window.kakao.maps) {
        fail("kakao_namespace_missing", sdkFailureText());
        return;
      }
      window.kakao.maps.load(initMap);
    };
    script.onerror = function () {
      clearTimeout(timeout);
      // 스크립트를 남겨두면 다음 시도에서 ④ 분기로 빠져 영영 재시도하지 못한다
      if (script.parentNode) script.parentNode.removeChild(script);
      fail("kakao_sdk_network_error", sdkFailureText());
    };
    document.head.appendChild(script);
  }

  function renderBody() {
    if (!active || !active.panelRow) return;
    var body = active.panelRow.querySelector("[data-panel-body]");
    if (!body) return;
    destroyCharts();
    active.map = null;
    if (active.tab === "trends") renderTrends(body);
    else if (active.tab === "location") renderLocation(body);
    else renderTrades(body);
  }

  function renderPanel() {
    var data = active.data;
    var panel = active.panelRow.querySelector("[data-panel]");
    var regionText = "";
    var regionEl = active.row.querySelector(".name-col div");
    if (regionEl) regionText = regionEl.textContent.trim();
    var place = [regionText, data.dongName].filter(Boolean).join(" · ");

    var areaBtns = data.availableAreas.map(function (a) {
      return '<button type="button" class="apt-inline-area" data-inline-area="' + a + '"' +
        ' aria-pressed="' + (Number(a) === Number(active.selectedArea)) + '">' +
        escapeHtml(areaLabel(a)) + '</button>';
    }).join("");

    var tabs = [["trades", "거래 내역"], ["trends", "가격 추이"], ["location", "위치"]]
      .map(function (t) {
        return '<button type="button" class="apt-inline-tab" data-inline-tab="' + t[0] + '"' +
          ' aria-selected="' + (active.tab === t[0]) + '">' + t[1] + '</button>';
      }).join("");

    panel.innerHTML =
      '<div class="apt-inline-head">' +
        '<div>' +
          '<strong class="apt-inline-title">' + escapeHtml(data.apartmentName) + '</strong>' +
          (place ? '<div class="apt-inline-place">' + escapeHtml(place) + '</div>' : '') +
        '</div>' +
        '<button type="button" class="apt-inline-close" data-inline-close="1">접기</button>' +
      '</div>' +
      (areaBtns ? '<div class="apt-inline-areas">' + areaBtns + '</div>' : '') +
      '<div class="apt-inline-tabs" role="tablist">' + tabs + '</div>' +
      '<div class="apt-inline-body" data-panel-body></div>';

    renderBody();
  }

  function buildPanelRow(row, contentHtml) {
    var colCount = row.cells.length || 5;
    var tr = document.createElement("tr");
    tr.className = "apt-detail-row";
    tr.id = "apt-detail-panel-" + (++panelSeq);
    var td = document.createElement("td");
    td.colSpan = colCount;
    td.innerHTML = '<div class="apt-inline-panel" data-panel>' + contentHtml + '</div>';
    tr.appendChild(td);
    row.parentNode.insertBefore(tr, row.nextSibling);
    return tr;
  }

  function openPanel(row) {
    var key = row.getAttribute("data-apt-key");
    if (!key) return;
    var sggCode = key.split("-")[0];

    closePanel();

    var panelRow = buildPanelRow(row, '<p class="apt-inline-loading">상세 정보를 불러오는 중…</p>');
    row.setAttribute("aria-expanded", "true");
    row.setAttribute("aria-controls", panelRow.id);
    row.classList.add("apt-row-open");

    active = {
      key: key, row: row, panelRow: panelRow, data: null, window: null,
      selectedArea: null, tab: "trades", shown: PAGE_SIZE,
      charts: { scatter: null, monthly: null }, map: null
    };

    loadDetail(sggCode, key).then(function (data) {
      if (!active || active.key !== key || !panelRow.isConnected) return;
      if (!data.availableAreas.length) {
        panelRow.querySelector("[data-panel]").innerHTML =
          '<p class="apt-inline-empty">수집된 거래 면적 정보가 없습니다.</p>' +
          '<p><a class="apt-inline-fallback" data-apt-key="' + escapeHtml(key) + '" href="apartment.html?key=' +
          encodeURIComponent(key) + '">기존 상세 페이지에서 보기</a></p>';
        return;
      }
      active.data = data;
      active.window = historyWindow(data);
      active.selectedArea = data.availableAreas[0];
      renderPanel();
    }).catch(function (err) {
      console.error("[inline_detail_failed]", err);
      if (!active || active.key !== key || !panelRow.isConnected) return;
      panelRow.querySelector("[data-panel]").innerHTML =
        '<p class="apt-inline-empty">상세 정보를 불러오지 못했습니다.</p>' +
        '<p><a class="apt-inline-fallback" data-apt-key="' + escapeHtml(key) + '" href="apartment.html?key=' +
        encodeURIComponent(key) + '">기존 상세 페이지에서 보기</a></p>';
    });
  }

  function toggleInlineApartmentDetail(row) {
    if (active && active.row === row) {
      closePanel();
      return;
    }
    openPanel(row);
  }

  // ── 이벤트 위임 ────────────────────────────────────────────────

  function handlePanelClick(target) {
    // fallback 링크는 기존 goToApartment로 넘겨 스크롤·필터 상태 저장을 유지한다
    var fallback = target.closest(".apt-inline-fallback");
    if (fallback) {
      if (typeof window.goToApartment === "function") {
        window.goToApartment(fallback.getAttribute("data-apt-key"));
        return true;
      }
      return false; // 함수가 없으면 평범한 링크로 동작
    }

    if (!active) return false;

    var closeBtn = target.closest("[data-inline-close]");
    if (closeBtn) { closePanel(); return true; }

    var moreBtn = target.closest("[data-inline-more]");
    if (moreBtn) {
      active.shown += PAGE_SIZE;
      renderBody();
      return true;
    }

    var areaBtn = target.closest("[data-inline-area]");
    if (areaBtn) {
      active.selectedArea = Number(areaBtn.getAttribute("data-inline-area"));
      active.shown = PAGE_SIZE;
      renderPanel();
      return true;
    }

    var tabBtn = target.closest("[data-inline-tab]");
    if (tabBtn) {
      active.tab = tabBtn.getAttribute("data-inline-tab");
      active.shown = PAGE_SIZE;
      renderPanel();
      return true;
    }

    return false;
  }

  function onContainerClick(e) {
    var target = e.target;

    // 패널 내부 조작은 행 토글로 번지지 않게 여기서 소비한다
    if (target.closest(".apt-detail-row")) {
      if (handlePanelClick(target)) e.preventDefault();
      return;
    }

    // 경고 아이콘 등 행 안의 다른 인터랙션은 패널을 열지 않는다
    if (target.closest(".warn-icon")) return;

    var row = target.closest("tr[data-apt-key]");
    if (!row) return;
    toggleInlineApartmentDetail(row);
  }

  function onContainerKeydown(e) {
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    var target = e.target;

    if (target.closest(".apt-detail-row")) return; // 패널 내부 버튼은 기본 동작에 맡긴다

    var row = target.closest("tr[data-apt-key]");
    if (!row || row !== target) return;
    e.preventDefault();
    toggleInlineApartmentDetail(row);
  }

  function watchContainer(el) {
    if (!el) return;
    el.addEventListener("click", onContainerClick);
    el.addEventListener("keydown", onContainerKeydown);

    // 순위 목록이 다시 그려지면(탭·집계·지역·신뢰도 변경) 패널 노드가 사라진다.
    // 기존 렌더러를 수정하지 않고 여기서 감지해 차트·지도를 정리한다.
    new MutationObserver(function () {
      if (active && (!active.panelRow.isConnected || !active.row.isConnected)) {
        destroyCharts();
        active.map = null;
        active = null;
      }
    }).observe(el, { childList: true, subtree: true });
  }

  // 탭·집계·필터·지역을 바꾸면 열려 있던 패널을 닫는다.
  // 목록이 다시 그려지는 경우는 MutationObserver가 잡지만, 탭 전환처럼
  // 컨테이너가 숨겨지기만 하는 경우에는 노드가 남으므로 여기서 직접 닫는다.
  var RESET_SELECTOR = [
    "#main-nav button",
    "#apt-sub-nav button",
    "#region-sub-nav button",
    "#apt-macro-container .chip",
    "#apt-submacro-container .chip",
    ".apt-micro-desktop button"
  ].join(",");

  function watchResets() {
    document.addEventListener("click", function (e) {
      if (!active) return;
      if (e.target.closest(RESET_SELECTOR)) closePanel();
    }, true);

    document.querySelectorAll('input[name="dataMode"], input[name="confFilter"]')
      .forEach(function (radio) {
        radio.addEventListener("change", function () {
          if (active) closePanel();
        }, true);
      });

    var select = document.querySelector(".apt-micro-select");
    if (select) {
      select.addEventListener("change", function () {
        if (active) closePanel();
      }, true);
    }
  }

  function start() {
    watchContainer(document.getElementById("apartment-nationwide-content"));
    watchContainer(document.getElementById("apartment-regional-content"));
    watchResets();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
