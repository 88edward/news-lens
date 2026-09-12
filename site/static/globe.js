/*
 * news-lens 글로브.
 *
 * 반드시 지키는 것:
 *  - WebGL 이 안 되면 글로브를 숨기고 국가 목록만 남긴다. 빈 화면은 용납하지 않는다.
 *  - prefers-reduced-motion 이면 자동 회전과 링을 끈다.
 *  - 640px 이하에서는 글로브를 40vh 로 줄이고 목록이 주인공이 된다 (CSS가 담당).
 *  - 패널은 Esc 로 닫히고, 포커스를 가둔다.
 *  - 로딩 중에는 스켈레톤을 보여준다.
 */
(function () {
  'use strict';

  var CFG = window.NEWS_LENS || {};
  var stage = document.getElementById('globe');
  var skeleton = document.getElementById('globe-skeleton');
  var fallbackNote = document.getElementById('globe-fallback-note');
  var panel = document.getElementById('panel');
  var panelTitle = document.getElementById('panel-title');
  var panelBody = document.getElementById('panel-body');
  var panelClose = document.getElementById('panel-close');
  var panelMore = document.getElementById('panel-more');

  var reduceMotion = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var globe = null;
  var lastFocused = null;
  var resumeTimer = null;

  // ── 폴백 ────────────────────────────────────────────────────────────

  function degrade(reason) {
    if (skeleton) skeleton.hidden = true;
    if (stage) stage.hidden = true;
    if (fallbackNote) fallbackNote.hidden = false;
    document.body.classList.add('no-globe');
    if (reason) {
      // 사용자에게는 이미 안내가 떠 있다. 콘솔은 개발자용.
      console.warn('[news-lens] 글로브를 띄우지 못했다:', reason);
    }
  }

  function webglSupported() {
    try {
      var canvas = document.createElement('canvas');
      return !!(
        window.WebGLRenderingContext
        && (canvas.getContext('webgl') || canvas.getContext('experimental-webgl'))
      );
    } catch (err) {
      return false;
    }
  }

  // ── 마커 매핑 ───────────────────────────────────────────────────────

  // 선형으로 하면 미국만 거대해져서 나머지가 안 보인다.
  function radiusOf(country, maxArticles) {
    var scaled = Math.sqrt(Math.max(0, country.article_count)) / Math.sqrt(maxArticles || 1);
    return 0.28 + scaled * 0.9;
  }

  // 논조: 부정(-10) → 주황/적, 중립(0) → 청색.
  function colorOf(country) {
    var tone = typeof country.avg_tone === 'number' ? country.avg_tone : 0;
    var t = Math.min(1, Math.max(0, -tone / 8));          // 0 중립 … 1 매우 부정
    var warm = [255, 122, 48];
    var cool = [86, 168, 255];
    var rgb = warm.map(function (w, i) { return Math.round(cool[i] + (w - cool[i]) * t); });
    var alpha = 0.55 + 0.4 * Math.min(1, Math.max(0, country.weight || 0));
    return 'rgba(' + rgb.join(',') + ',' + alpha.toFixed(2) + ')';
  }

  function ringCountries(countries) {
    if (reduceMotion) return [];
    var threshold = CFG.ringSurgeThreshold || 1.8;
    return countries
      .filter(function (c) { return (c.surge || 0) > threshold; })
      .sort(function (a, b) { return b.surge - a.surge; })
      .slice(0, CFG.maxRings || 3);
  }

  // ── 패널 ────────────────────────────────────────────────────────────

  var FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function openPanel(country) {
    lastFocused = document.activeElement;
    panelTitle.textContent = country.name_ko;
    panelBody.innerHTML = '<p class="muted">불러오는 중…</p>';
    panelMore.href = CFG.countryPage(country.iso2);
    panel.hidden = false;
    document.body.classList.add('panel-open');
    panelClose.focus();

    fetch(CFG.countryUrl(country.iso2))
      .then(function (res) {
        if (!res.ok) throw new Error(res.status);
        return res.json();
      })
      .then(function (data) { renderPanel(data); })
      .catch(function (err) {
        panelBody.innerHTML =
          '<p class="notice">이 나라의 사건을 불러오지 못했다. '
          + '<a href="' + CFG.countryPage(country.iso2) + '">전체 페이지에서 보기</a></p>';
        console.warn('[news-lens]', err);
      });
  }

  function toneClass(tone) {
    if (tone === null || tone === undefined) return 'tone-neutral';
    if (tone <= -5) return 'tone-verynegative';
    if (tone <= -1.5) return 'tone-negative';
    if (tone < 1.5) return 'tone-neutral';
    return 'tone-positive';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function renderPanel(data) {
    if (!data.events || !data.events.length) {
      panelBody.innerHTML = '<p class="muted">오늘 이 나라의 사건이 없다.</p>';
      return;
    }
    var html = data.events.map(function (e) {
      return ''
        + '<article class="panel-event">'
        + '<h3><a href="' + CFG.eventPage(e.id) + '">' + escapeHtml(e.headline) + '</a></h3>'
        + '<p class="meta">'
        + '<span class="badge ' + toneClass(e.tone) + '">논조 ' + escapeHtml(e.tone) + '</span>'
        + (e.category ? '<span class="badge">' + escapeHtml(e.category) + '</span>' : '')
        + '<span class="muted">매체 ' + escapeHtml(e.source_count) + '곳</span>'
        + '</p>'
        + (e.summary ? '<p class="summary">' + escapeHtml(e.summary) + '</p>' : '')
        + '</article>';
    }).join('');
    panelBody.innerHTML = html;
    panelBody.focus();
  }

  function closePanel() {
    panel.hidden = true;
    document.body.classList.remove('panel-open');
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  function trapFocus(event) {
    if (panel.hidden || event.key !== 'Tab') return;
    var items = Array.prototype.filter.call(
      panel.querySelectorAll(FOCUSABLE),
      function (el) { return el.offsetParent !== null; }
    );
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !panel.hidden) {
      event.preventDefault();
      closePanel();
    }
    trapFocus(event);
  });
  if (panelClose) panelClose.addEventListener('click', closePanel);

  // 목록에서도 같은 패널을 열 수 있게 한다 (글로브가 없어도 동작).
  document.addEventListener('click', function (event) {
    var row = event.target.closest && event.target.closest('tr[data-iso2]');
    if (!row || event.target.closest('a')) return;
    var iso2 = row.getAttribute('data-iso2');
    var match = (window.__NL_COUNTRIES || []).filter(function (c) {
      return c.iso2 === iso2;
    })[0];
    if (match) openPanel(match);
  });

  // ── 글로브 ──────────────────────────────────────────────────────────

  function labelBudget() {
    var max = CFG.maxLabels || 12;
    return window.innerWidth <= 640 ? Math.min(6, max) : max;
  }

  function pauseRotation() {
    if (!globe || reduceMotion) return;
    globe.controls().autoRotate = false;
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(function () {
      if (globe) globe.controls().autoRotate = true;
    }, 5000);
  }

  function buildGlobe(data) {
    var countries = data.countries || [];
    var maxArticles = countries.reduce(function (max, c) {
      return Math.max(max, c.article_count || 0);
    }, 1);

    globe = window.Globe()(stage)
      .backgroundColor('rgba(0,0,0,0)')
      .globeImageUrl('static/earth-night.jpg')
      .showAtmosphere(true)
      .atmosphereColor('#2b5d87')
      .atmosphereAltitude(0.16)
      .pointsData(countries)
      .pointLat('lat')
      .pointLng('lng')
      .pointRadius(function (c) { return radiusOf(c, maxArticles); })
      .pointAltitude(function (c) { return 0.008 + (c.weight || 0) * 0.06; })
      .pointColor(colorOf)
      .pointLabel(function (c) {
        return '<b>' + escapeHtml(c.name_ko) + '</b><br>'
          + '사건 ' + c.event_count + ' · 기사 ' + c.article_count
          + (c.top_event ? '<br>' + escapeHtml(c.top_event.headline) : '');
      })
      .onPointClick(function (c) {
        pauseRotation();
        openPanel(c);
      })
      // 라벨은 labelsData 가 아니라 DOM 레이어로 그린다.
      // three-globe 의 labelsData 는 TextGeometry + helvetiker 폰트라
      // 한글 글리프가 없어 아무것도 안 그려진다. 조용히 비어 보이므로
      // 원인을 찾기 어렵다 — DOM 라벨은 브라우저 폰트를 그대로 쓴다.
      // 전부 켜면 겹쳐서 못 읽으니 상위 N개만.
      // 좁은 화면은 같은 개수를 켜도 겹치므로 더 줄인다.
      .htmlElementsData(countries.slice(0, labelBudget()))
      .htmlLat('lat')
      .htmlLng('lng')
      .htmlAltitude(0.02)
      .htmlElement(function (c) {
        var el = document.createElement('div');
        el.className = 'globe-label';
        el.textContent = c.name_ko;
        el.title = c.name_ko + ' — 사건 ' + c.event_count + '건';
        el.addEventListener('click', function () {
          pauseRotation();
          openPanel(c);
        });
        return el;
      });

    var rings = ringCountries(countries);
    if (rings.length) {
      globe
        .ringsData(rings)
        .ringLat('lat')
        .ringLng('lng')
        .ringColor(function () { return function (t) { return 'rgba(255,150,60,' + (1 - t) + ')'; }; })
        .ringMaxRadius(5)
        .ringPropagationSpeed(1.6)
        .ringRepeatPeriod(900);
    }

    globe.controls().autoRotate = !reduceMotion;
    globe.controls().autoRotateSpeed = 0.35;
    globe.controls().enableDamping = true;

    stage.addEventListener('pointerdown', pauseRotation);
    stage.addEventListener('wheel', pauseRotation, { passive: true });

    function resize() {
      globe.width(stage.clientWidth).height(stage.clientHeight);
      globe.htmlElementsData(countries.slice(0, labelBudget()));
    }
    resize();
    window.addEventListener('resize', resize);

    // 종횡비가 잡힌 뒤에 시점을 정한다. 먼저 부르면 캔버스 크기가 0이라
    // 카메라 거리가 엉뚱하게 잡힌다.
    // 대서양 위 — 유럽·아프리카·남북미가 한눈에 들어온다.
    globe.pointOfView({ lat: 18, lng: 10, altitude: 2.6 }, 0);

    if (skeleton) skeleton.hidden = true;
    window.__NL_GLOBE = globe;   // 디버깅용
  }

  // ── 시작 ────────────────────────────────────────────────────────────

  fetch(CFG.dataUrl)
    .then(function (res) {
      if (!res.ok) throw new Error('globe.json ' + res.status);
      return res.json();
    })
    .then(function (data) {
      window.__NL_COUNTRIES = data.countries || [];
      if (!stage) return;
      if (!webglSupported()) return degrade('WebGL 미지원');
      if (typeof window.Globe !== 'function') return degrade('globe.gl 로드 실패');
      try {
        buildGlobe(data);
      } catch (err) {
        degrade(err);
      }
    })
    .catch(function (err) {
      degrade(err);
    });
})();
