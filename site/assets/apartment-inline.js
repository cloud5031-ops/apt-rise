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

  function axisMonths(data, trades) {
    var hc = data.historyCoverage;
    var start, end;
    if (hc && hc.startMonth && hc.endMonth) {
      start = hc.startMonth;
      end = hc.endMonth;
    } else {
      var ms = trades.map(monthOf).filter(Boolean).sort();
      if (!ms.length) return [];
      start = ms[0];
      end = ms[ms.length - 1];
    }
    var out = [];
    var m = start;
    // 거래가 없는 달도 X축 흐름을 유지한다 (최대 36칸)
    for (var i = 0; i < 36 && m <= end; i++) {
      out.push(m);
      m = shiftMonth(m, 1);
    }
    return out;
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

  function validTrades(data, area) {
    return data.transactions.filter(function (t) {
      return t.cancellationStatus !== "CANCELLED" && Number(t.exclusiveArea) === Number(area);
    }).sort(function (a, b) {
      return String(b.contractDate || "").localeCompare(String(a.contractDate || ""));
    });
  }

  function coverageNotice(data) {
    var hc = data.historyCoverage;
    if (hc && hc.complete === true) {
      return '<p class="apt-inline-note">최근 3년(36개월) 실거래 내역입니다.</p>';
    }
    return '<p class="apt-inline-note">현재 수집된 기간의 거래만 표시됩니다.</p>';
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
        '<td>' + escapeHtml(t.exclusiveArea) + '㎡</td>' +
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
      '<div class="apt-inline-chart"><canvas data-chart="scatter"></canvas></div>' +
      '<div class="apt-inline-chart"><canvas data-chart="monthly"></canvas></div>';

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

    var yMax = maxPrice * 1.05;
    var yMin = minPrice * 0.95;

    var scatterCanvas = body.querySelector('canvas[data-chart="scatter"]');
    active.charts.scatter = new window.Chart(scatterCanvas.getContext("2d"), {
      type: "scatter",
      data: {
        datasets: [
          {
            label: "중개거래",
            data: scatterData.filter(function (d) { return d.raw.dealType !== "직거래"; }),
            backgroundColor: "#2563c9", pointRadius: 5, pointHoverRadius: 7
          },
          {
            label: "직거래",
            data: scatterData.filter(function (d) { return d.raw.dealType === "직거래"; }),
            backgroundColor: "#d6293e", pointStyle: "rectRot", pointRadius: 6, pointHoverRadius: 8
          }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var t = ctx.raw.raw;
                return formatDate(t.contractDate) + " | " + formatPriceWon(getPriceWon(t)) +
                  " | " + t.floor + "층 | " + t.dealType;
              }
            }
          }
        },
        scales: {
          x: {
            type: "linear",
            ticks: {
              callback: function (val) {
                var d = new Date(val);
                return String(d.getFullYear()).slice(2) + "." +
                  String(d.getMonth() + 1).padStart(2, "0");
              },
              maxTicksLimit: 6
            }
          },
          y: { max: yMax, min: yMin, ticks: { callback: function (v) { return formatPriceWon(v); } } }
        }
      }
    });

    var months = axisMonths(active.data, trades);
    var medians = [];
    var volumes = [];
    months.forEach(function (m) {
      var prices = (monthlyGroups[m] || []).slice().sort(function (a, b) { return a - b; });
      if (!prices.length) { medians.push(null); volumes.push(0); return; }
      var len = prices.length;
      medians.push(len % 2 === 0 ? (prices[len / 2 - 1] + prices[len / 2]) / 2 : prices[Math.floor(len / 2)]);
      volumes.push(len);
    });

    var monthlyCanvas = body.querySelector('canvas[data-chart="monthly"]');
    active.charts.monthly = new window.Chart(monthlyCanvas.getContext("2d"), {
      type: "line",
      data: {
        labels: months.map(function (m) { return m.slice(0, 4) + "." + m.slice(4); }),
        datasets: [
          {
            type: "line", label: "실거래 중위가격", data: medians,
            borderColor: "#2563c9", backgroundColor: "#2563c9",
            yAxisID: "y", tension: 0.1, pointRadius: 3, spanGaps: true
          },
          {
            type: "bar", label: "거래 건수", data: volumes,
            backgroundColor: "rgba(107, 118, 132, 0.3)", yAxisID: "y1"
          }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          tooltip: {
            callbacks: {
              label: function (ctx) {
                if (ctx.datasetIndex === 0) return "실거래 중위가격: " + formatPriceWon(ctx.raw);
                return "거래 건수: " + ctx.raw + "건";
              },
              afterLabel: function (ctx) {
                if (ctx.datasetIndex !== 0) return "";
                var i = ctx.dataIndex;
                if (i <= 0 || medians[i] == null || medians[i - 1] == null) return "";
                if (volumes[i - 1] < 2 || volumes[i] < 2) return "실거래 중위가격 변화율: 표본 부족";
                var rate = ((medians[i] - medians[i - 1]) / medians[i - 1] * 100).toFixed(2);
                return "실거래 중위가격 변화율: " + (rate > 0 ? "+" : "") + rate + "%";
              }
            }
          }
        },
        scales: {
          y: {
            type: "linear", position: "left", max: yMax, min: yMin,
            ticks: { callback: function (v) { return formatPriceWon(v); } }
          },
          y1: {
            type: "linear", position: "right", min: 0,
            grid: { drawOnChartArea: false }, ticks: { stepSize: 1 }
          }
        }
      }
    });
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
    function showMapError(reason, text) {
      console.error("Inline map failed: " + reason);
      msgEl.textContent = text;
      msgEl.hidden = false;
      mapEl.style.display = "none";
    }

    if (loc.geocodeStatus !== "valid" || !Number.isFinite(lat) || !Number.isFinite(lng) || !lat || !lng) {
      showMapError("coordinates_missing", "지도에 표시할 단지 좌표 정보가 없습니다.");
      return;
    }
    if (!window.APP_CONFIG || !window.APP_CONFIG.kakaoMapJavaScriptKey) {
      showMapError("config_missing", "지도 서비스를 불러오지 못했습니다.");
      return;
    }
    var key = window.APP_CONFIG.kakaoMapJavaScriptKey;
    if (!key.trim() || key === "__KAKAO_JAVASCRIPT_KEY__") {
      showMapError("javascript_key_missing", "지도 서비스를 불러오지 못했습니다.");
      return;
    }

    var token = active.key;
    function initMap() {
      if (!active || active.key !== token || !mapEl.isConnected) return;
      try {
        var pos = new window.kakao.maps.LatLng(lat, lng);
        var map = new window.kakao.maps.Map(mapEl, { center: pos, level: 3 });
        active.map = map;
        new window.kakao.maps.Marker({ position: pos }).setMap(map);
        new window.kakao.maps.CustomOverlay({
          position: pos, yAnchor: 1,
          content: '<div class="apt-inline-map-label">' + escapeHtml(data.apartmentName) + '</div>'
        }).setMap(map);
        // 패널이 숨겨진 상태로 생성됐을 수 있으므로 표시 후 다시 배치한다
        setTimeout(function () {
          if (!active || active.map !== map) return;
          map.relayout();
          map.setCenter(pos);
        }, 100);
      } catch (e) {
        showMapError("map_init_failed", "지도 서비스를 불러오지 못했습니다.");
      }
    }

    if (window.kakao && window.kakao.maps && window.kakao.maps.load) {
      window.kakao.maps.load(initMap);
      return;
    }

    var existing = document.querySelector('script[data-inline-kakao]');
    if (existing) {
      existing.addEventListener("load", function () {
        if (window.kakao && window.kakao.maps) window.kakao.maps.load(initMap);
      });
      existing.addEventListener("error", function () {
        showMapError("kakao_sdk_network_error", "지도 서비스를 불러오지 못했습니다.");
      });
      return;
    }

    var script = document.createElement("script");
    script.src = "https://dapi.kakao.com/v2/maps/sdk.js?appkey=" +
      encodeURIComponent(key) + "&autoload=false";
    script.setAttribute("data-inline-kakao", "1");
    var timeout = setTimeout(function () {
      showMapError("kakao_sdk_timeout", "지도 서비스를 불러오지 못했습니다.");
    }, 15000);
    script.onload = function () {
      clearTimeout(timeout);
      if (!window.kakao || !window.kakao.maps) {
        showMapError("kakao_namespace_missing", "지도 서비스를 불러오지 못했습니다.");
        return;
      }
      window.kakao.maps.load(initMap);
    };
    script.onerror = function () {
      clearTimeout(timeout);
      showMapError("kakao_sdk_network_error", "지도 서비스를 불러오지 못했습니다.");
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
      key: key, row: row, panelRow: panelRow, data: null,
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
