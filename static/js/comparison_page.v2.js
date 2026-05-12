/* Généré depuis app.py — logique page /comparison */
(function () {
    const catalog = window.__CMP__.catalog;
    const brandSelect = document.getElementById('brandSelect');
    const modelSelect = document.getElementById('modelSelect');
    if (brandSelect && modelSelect) {
      brandSelect.addEventListener('change', () => {
        const models = catalog[brandSelect.value] || [];
        modelSelect.innerHTML = '';
        models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m; opt.textContent = m;
          modelSelect.appendChild(opt);
        });
        // Mettre à jour l'image dès que la marque change
        if (typeof window.displayProduct === 'function' && modelSelect.value) {
          window.displayProduct(modelSelect.value, brandSelect.value || null);
        }
      });
    }
    const qs = new URLSearchParams({
      brand:(brandSelect && brandSelect.value) || window.__CMP__.selectedBrand,
      model:(modelSelect && modelSelect.value) || window.__CMP__.selectedModel,
      validated_only:'true'
    });
    const rows = document.getElementById('rows');
    const jobStatus = document.getElementById('jobStatus');
    const clockHealth = document.getElementById('clockHealth');
    const scoreTotal = document.getElementById('scoreTotal');
    const scoreGrade = document.getElementById('scoreGrade');
    const scoreMeta = document.getElementById('scoreMeta');
    const copySyncLinkBtn = document.getElementById('copySyncLink');
    const shareWhatsAppBtn = document.getElementById('shareWhatsApp');
    let refreshModeActive = false;
    let lastRenderedItems = null;
    let retryStatusTimer = null;
    let lastHealthSnapshot = null;

    function setRefreshMode(active) {
      const on = !!active;
      if (on === refreshModeActive) return;
      refreshModeActive = on;
      if (on) console.info('[UI] refresh mode active');
    }
    function renderJobStatus(s) {
      if (!jobStatus) return;
      const running = !!(s && s.running);
      setRefreshMode(running);
      jobStatus.className = 'upd-badge ' + (running ? 'job-run' : 'job-idle');
      if (running) {
        jobStatus.textContent = '🟢 Synchronisation en cours...';
        if (clockHealth) {
          const warn = (((s && s.data_freshness) || {}).warnings || []);
          const hasStale = Array.isArray(warn) && warn.length > 0;
          clockHealth.className = 'upd-badge ' + (hasStale ? 'clock-warning' : 'clock-ok');
          clockHealth.textContent = hasStale ? '🟠 Mise à jour des données en cours' : '🟢 Synchronisation en cours...';
        }
      } else {
        const endAt = (s && s.last_end_at) ? String(s.last_end_at).slice(11,16) : '';
        jobStatus.textContent = endAt ? `dernier run ${endAt}` : 'idle';
      }
    }
    let jobStatusTimer = null;
    function refreshJobStatus() {
      fetchWithTimeout('/api/update/fr/status', 4000)
        .then(r => {
          if (r.status === 401) {
            if (jobStatusTimer) {
              clearInterval(jobStatusTimer);
              jobStatusTimer = null;
            }
            if (jobStatus) {
              jobStatus.className = 'upd-badge job-idle';
              jobStatus.textContent = 'session expiree';
            }
            return null;
          }
          return r.json();
        })
        .then(data => {
          if (data) renderJobStatus(data);
        })
        .catch(() => {
          if (refreshModeActive && jobStatus) {
            jobStatus.className = 'upd-badge job-run';
            jobStatus.textContent = '🟢 Synchronisation en cours...';
          }
          if (retryStatusTimer) clearTimeout(retryStatusTimer);
          console.info('[UI] retrying status');
          retryStatusTimer = setTimeout(refreshJobStatus, 5000);
        });
    }
    refreshJobStatus();
    jobStatusTimer = setInterval(refreshJobStatus, 15000);

    function gradeClass(grade) {
      if (grade === 'Premium') return 'score-grade g-premium';
      if (grade === 'Fiable') return 'score-grade g-fiable';
      if (grade === 'A surveiller') return 'score-grade g-watch';
      return 'score-grade g-risk';
    }
    function clampMinutes(mins) {
      return Math.min(Math.max(0, Number(mins) || 0), 999);
    }
    function parseDateLoose(raw) {
      const txt = String(raw || '').trim();
      if (!txt) return null;
      const d = new Date(txt.replace(' ', 'T'));
      return Number.isNaN(d.getTime()) ? null : d;
    }
    function formatSyncAge(rawDate) {
      const dt = parseDateLoose(rawDate);
      if (!dt) return 'Synchro en attente';
      const mins = clampMinutes((Date.now() - dt.getTime()) / 60000);
      if (mins > 180) return 'Synchro en attente';
      if (mins > 120) return 'Synchro en cours';
      return `il y a ${Math.round(mins)} min`;
    }
    function setLiveDataBadge(isFresh) {
      const badge = document.getElementById('liveDataBadge');
      if (!badge) return;
      badge.className = 'upd-badge ' + (isFresh ? 'live-good' : 'live-warn');
      badge.textContent = isFresh
        ? '🟢 LIVE DATA — Marché analysé en temps réel'
        : '🟠 Données en cours de synchronisation';
    }
    function showSkeletonRows() {
      if (!rows) return;
      rows.innerHTML = Array.from({ length: 4 }).map(function() {
        return '<tr class="cmp-skeleton"><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td><td><span class="sb-skeleton-line"></span></td></tr>';
      }).join('');
    }
    function refreshHealthSignals() {
      fetchWithTimeout('/health', 4000)
        .then(function(r) { return r.json(); })
        .then(function(h) {
          lastHealthSnapshot = h || {};
          const fresh = ((h || {}).data_freshness || {});
          const fr = fresh.market_fr_csv || {};
          const src = fresh.market_fr_sources_csv || {};
          const stale = !!(fr.stale || src.stale);
          setLiveDataBadge(!stale);
          const lastSync = document.getElementById('lastSyncHuman');
          if (lastSync) {
            const label = String((h || {}).last_sync_human || '').trim() || formatSyncAge((((h || {}).serpapi_sync || {}).last_run || ((h || {}).last_refresh || {}).updated_at));
            lastSync.textContent = label;
          }
          const totalShops = document.getElementById('total-boutiques');
          if (totalShops) {
            const n = Number(src.distinct_shops || 0);
            if (n > 0) totalShops.textContent = String(n);
          }
        })
        .catch(function() {
          setLiveDataBadge(false);
        });
    }
    function currentStateParams() {
      const p = new URLSearchParams(window.location.search);
      p.set('brand', (brandSelect && brandSelect.value) || window.__CMP__.selectedBrand);
      p.set('model', (modelSelect && modelSelect.value) || window.__CMP__.selectedModel);
      p.delete('include_excluded');
      p.delete('pqs');
      p.delete('perf');
      return p;
    }
    function syncUrlState() {
      const p = currentStateParams();
      window.history.replaceState(null, '', `${window.location.pathname}?${p.toString()}`);
    }

    function refreshCommercialScore() {
      const brand = (brandSelect && brandSelect.value) || window.__CMP__.selectedBrand;
      const model = (modelSelect && modelSelect.value) || window.__CMP__.selectedModel;
      const s = new URLSearchParams({
        brand,
        model,
        product_quality_score: '80',
        performance_score: '78',
      });
      fetchWithTimeout('/api/scorecard/fr?' + s.toString(), 4000)
        .then(r => r.json())
        .then(d => {
          const total = Number(d.total_score || 0).toFixed(2);
          const grade = String(d.grade || 'Risque eleve');
          const n = Number(d.items_count || 0);
          if (scoreTotal) scoreTotal.textContent = `Score global commercial: ${total}/100`;
          if (scoreGrade) {
            scoreGrade.className = gradeClass(grade);
            scoreGrade.textContent = grade;
          }
          if (scoreMeta) scoreMeta.textContent = n > 0 ? `${n} ligne(s) validée(s) utilisées` : 'Aucune ligne valide';
        })
        .catch(() => {
          if (refreshModeActive) {
            console.info('[UI] cached data preserved');
            return;
          }
          if (scoreTotal) scoreTotal.textContent = 'Score global commercial: indisponible';
          if (scoreGrade) {
            scoreGrade.className = 'score-grade g-risk';
            scoreGrade.textContent = 'N/A';
          }
          if (scoreMeta) scoreMeta.textContent = 'Erreur de calcul scorecard';
        });
    }
    refreshCommercialScore();
    if (brandSelect) brandSelect.addEventListener('change', refreshCommercialScore);
    if (modelSelect) modelSelect.addEventListener('change', refreshCommercialScore);
    if (brandSelect) brandSelect.addEventListener('change', syncUrlState);
    if (modelSelect) modelSelect.addEventListener('change', syncUrlState);
    if (copySyncLinkBtn) {
      copySyncLinkBtn.addEventListener('click', async () => {
        try {
          syncUrlState();
          await navigator.clipboard.writeText(window.location.href);
          copySyncLinkBtn.textContent = 'Lien copié';
          setTimeout(() => { copySyncLinkBtn.textContent = 'Copier lien mobile'; }, 1400);
        } catch {
          copySyncLinkBtn.textContent = 'Copie impossible';
          setTimeout(() => { copySyncLinkBtn.textContent = 'Copier lien mobile'; }, 1400);
        }
      });
    }
    if (shareWhatsAppBtn) {
      shareWhatsAppBtn.addEventListener('click', () => {
        try {
          syncUrlState();
          const shareUrl = window.location.href;
          // Lien texte uniquement (pas d'image envoyée par le bouton).
          const txt = encodeURIComponent(shareUrl);
          const waUrl = `https://wa.me/?text=${txt}`;
          window.open(waUrl, '_blank');
        } catch {
          // no-op
        }
      });
    }
    // Propager pqs/perf dans l'URL à chaque soumission du formulaire.
    const form = document.querySelector('form.f');
    if (form) {
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const p = currentStateParams();
        window.location.search = p.toString();
      });
    }

    function escAttr(s) {
      return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    }
    function recBadgeClass(r) {
      const m = {
        '⭐ MEILLEUR PRIX': 'badge-rec rec-gold',
        '💰 PRIX LE PLUS BAS': 'badge-rec rec-gm',
        '✅ PRIX MARCHÉ': 'badge-rec rec-mkt',
        '⚠️ PRIX ÉLEVÉ': 'badge-rec rec-high',
        '👍 BON PRIX': 'badge-rec rec-ok'
      };
      return m[r] || 'badge-rec rec-ok';
    }
    function posBadgeClass(p) {
      const m = {
        'Top 10%': 'badge-pos pos-t10',
        'Top 25%': 'badge-pos pos-t25',
        'Milieu de gamme': 'badge-pos pos-mid',
        'Haut de gamme': 'badge-pos pos-hi'
      };
      return m[p] || 'badge-pos pos-mid';
    }
    function sneakerModelId(brand, model) {
      return String(brand || '').trim() + '|' + String(model || '').trim();
    }
    function cmpCloseHistory() {
      const ho = document.getElementById('modal-history-overlay');
      if (ho) ho.style.display = 'none';
    }
    window.__cmpCloseHistory = cmpCloseHistory;
    function cmpRenderHistory(hist, trend) {
      hist = hist || {};
      trend = trend || {};
      const points = hist.history || [];
      const trendIcons = { hausse: '📈', baisse: '📉', stable: '➡️' };
      const trendColors = { hausse: '#ff6b6b', baisse: '#00ff88', stable: '#ffd700' };
      const trKey = trend.trend;
      const icon = trendIcons[trKey] || '—';
      const color = trendColors[trKey] || '#aaa';
      const chg = trend.change_pct != null ? Number(trend.change_pct) : 0;
      let svgChart = '<p style="color:#666;font-size:13px">Pas assez de données pour le graphique (min. 2 snapshots)</p>';
      if (points.length >= 2) {
        const prices = points.map(p => Number(p.price_avg));
        const minP = Math.min(...prices);
        const maxP = Math.max(...prices);
        const range = (maxP - minP) || 1;
        const W = 520, H = 80;
        const pts = prices.map((p, i) => {
          const x = (i / (prices.length - 1)) * W;
          const y = H - ((p - minP) / range) * (H - 10) - 5;
          return x.toFixed(1) + ',' + y.toFixed(1);
        }).join(' ');
        const dates = points.map(p => (p.recorded_at && String(p.recorded_at).substring(0, 10)) || '');
        let circles = '';
        for (let i = 0; i < points.length; i++) {
          const x = (i / (prices.length - 1)) * W;
          const y = H - ((prices[i] - minP) / range) * (H - 10) - 5;
          const dshort = dates[i] ? dates[i].substring(5) : '';
          circles += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="3" fill="#00ff88"/>';
          circles += '<text x="' + x.toFixed(1) + '" y="' + (H + 15) + '" font-size="9" fill="#666" text-anchor="middle">' + escAttr(dshort) + '</text>';
        }
        svgChart = '<svg width="100%" viewBox="0 0 ' + W + ' ' + (H + 20) + '" style="margin:12px 0;overflow:visible">' +
          '<polyline points="' + pts + '" fill="none" stroke="#00ff88" stroke-width="2"/>' + circles + '</svg>';
      }
      const hb = document.getElementById('history-modal-body');
      if (!hb) return;
      const tlab = trKey != null ? String(trKey) : '—';
      hb.innerHTML = '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin-bottom:16px">' +
        '<span>Tendance : <strong style="color:' + color + '">' + icon + ' ' + escAttr(tlab) + '</strong></span>' +
        '<span>Variation : <strong style="color:' + color + '">' + (chg >= 0 ? '+' : '') + chg + '%</strong></span>' +
        '<span>Min 30j : <strong style="color:#00ff88">' + escAttr(String(trend.min_30d != null ? trend.min_30d : '—')) + '\u00a0€</strong></span>' +
        '<span>Max 30j : <strong style="color:#00ff88">' + escAttr(String(trend.max_30d != null ? trend.max_30d : '—')) + '\u00a0€</strong></span>' +
        '<span>Snapshots : <strong>' + escAttr(String(trend.nb_snapshots != null ? trend.nb_snapshots : 0)) + '</strong></span>' +
        '</div>' + svgChart +
        (points.length === 0 ? '<p style="color:#666;font-size:13px">Aucun historique pour l’instant.</p>' : '');
    }
    function cmpOpenHistory(modelId) {
      const ho = document.getElementById('modal-history-overlay');
      if (!ho || !modelId) return;
      ho.style.display = 'flex';
      const ht = document.getElementById('history-modal-title');
      if (ht) ht.textContent = String(modelId).split('|').join(' ');
      const hb = document.getElementById('history-modal-body');
      if (hb) hb.innerHTML = '<p style="color:#666">Chargement...</p>';
      const enc = encodeURIComponent(modelId);
      Promise.all([
        fetch('/api/sneakers/' + enc + '/history?days=30', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error('h'); return r.json(); }),
        fetch('/api/sneakers/' + enc + '/trend', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error('t'); return r.json(); })
      ]).then((pair) => cmpRenderHistory(pair[0], pair[1]))
        .catch(() => { if (hb) hb.innerHTML = '<p style="color:#ff4444">Historique non disponible</p>'; });
    }

    const WA_NUMBER = window.__CMP__.waNumber;
    const IS_ADMIN = !!window.__CMP__.isAdmin;

    // ── KPI Bar : models count from catalog ──────────────────────────────────
    (function initKpiBar() {
      const totalModels = Object.values(catalog || {}).reduce(function(s, a) { return s + (Array.isArray(a) ? a.length : 0); }, 0);
      const kpiModels = document.getElementById('kpi-models');
      if (kpiModels && totalModels > 0) kpiModels.textContent = totalModels;
    })();

    // ── Market Health : computed from items after fetch ──────────────────────
    function updateMarketHealth(items) {
      const arr = Array.isArray(items) ? items.filter(Boolean) : [];
      if (!arr.length) return; // HTML defaults (Stable / Moyenne / 87/100) remain visible

      // ── Trend ──
      const states = arr.map(function(it) { return String(it.market_state || 'stable'); });
      const unstable = states.filter(function(s) { return s === 'unstable'; }).length;
      const variable = states.filter(function(s) { return s === 'variable'; }).length;
      let trend = 'Stable', trendColor = '#4dff9f';
      if (unstable > arr.length * 0.25) { trend = 'Volatile'; trendColor = '#f87171'; }
      else if (variable > arr.length * 0.40) { trend = 'Variable'; trendColor = '#ffd700'; }

      // ── Liquidité ──
      const avgSrc = arr.reduce(function(s, it) { return s + Number(it.source_count || 0); }, 0) / arr.length;
      const liq = avgSrc >= 15 ? 'Élevée' : avgSrc >= 6 ? 'Moyenne' : 'Faible';
      const liqColor = liq === 'Élevée' ? '#4dff9f' : liq === 'Moyenne' ? '#ffd700' : '#f87171';

      // ── Score confiance — trust_score prioritaire sur score pipeline ──
      const scores = arr.map(function(it) {
        return Number((it.trust_report || {}).trust_score || it.score || 0);
      }).filter(function(s) { return s > 0; });
      const avgScore = scores.length ? Math.round(scores.reduce(function(a, b) { return a + b; }, 0) / scores.length) : 0;
      const confText = avgScore > 0 ? avgScore + '/100' : null; // null = keep HTML fallback "87/100"

      // ── Update mkt-health bar ──
      const el = function(id) { return document.getElementById(id); };
      const mktTrend = el('mkt-trend');
      const mktLiq = el('mkt-liq');
      const mktConf = el('mkt-conf');
      if (mktTrend) { mktTrend.textContent = trend; mktTrend.style.color = trendColor; }
      if (mktLiq) { mktLiq.textContent = liq; mktLiq.style.color = liqColor; }
      if (mktConf && confText) {
        mktConf.textContent = confText;
        mktConf.classList.remove('trust-high', 'trust-mid', 'trust-low');
        if (avgScore >= 83) mktConf.classList.add('trust-high');
        else if (avgScore >= 65) mktConf.classList.add('trust-mid');
        else mktConf.classList.add('trust-low');
      }
      const modelSources = document.getElementById('model-sources');
      if (modelSources) {
        const total = arr.reduce(function(s, it) { return s + Number(it.source_count || 0); }, 0);
        modelSources.textContent = String(total);
      }
      // P6 : mise à jour mention source count + badge fraîcheur
      const srcMention = document.getElementById('sourceCountMention');
      if (srcMention) {
        const totalSrc = arr.reduce(function(s, it) { return s + Number(it.source_count || 0); }, 0);
        srcMention.textContent = totalSrc > 0 ? String(totalSrc) : '\u2014';
      }
      const freshBadge = document.getElementById('freshnessBadge');
      if (freshBadge && arr.length > 0) {
        const hasFreshData = arr.some(it => {
          const upd = String(it.updated_at || '');
          if (!upd) return false;
          try { return (Date.now() - new Date(upd).getTime()) < 4 * 3600 * 1000; } catch(e) { return false; }
        });
        if (hasFreshData) {
          freshBadge.textContent = '\uD83D\uDFE2 Donn\u00e9es fra\u00eeches';
          freshBadge.style.background = '#071a0c';
          freshBadge.style.color = '#4dff9f';
        }
      }
      const modelLastCheck = document.getElementById('model-last-check');
      if (modelLastCheck) {
        const sync = (lastHealthSnapshot || {}).serpapi_sync || {};
        const refresh = (lastHealthSnapshot || {}).last_refresh || {};
        modelLastCheck.textContent = formatSyncAge(sync.last_run || refresh.updated_at || refresh.last_end_at);
      }

      // ── Insight cards : Résumé marché ──
      const avgs = arr.map(function(it) { return Number(it.price_avg || 0); }).filter(function(v) { return v > 0; });
      const globalAvg = avgs.length ? avgs.reduce(function(a, b) { return a + b; }, 0) / avgs.length : 0;
      const spreads = arr.map(function(it) {
        const mn = Number(it.price_min || 0), mx = Number(it.price_max || 0), av = Number(it.price_avg || 0);
        return av > 0 ? (mx - mn) / av * 100 : 0;
      }).filter(function(s) { return s > 0; });
      const avgSpread = spreads.length ? Math.round(spreads.reduce(function(a, b) { return a + b; }, 0) / spreads.length) : 0;

      const insAvg = el('ins-avg');
      const insSpread = el('ins-spread');
      const insConf = el('ins-conf');
      if (insAvg && globalAvg > 0) insAvg.textContent = globalAvg.toFixed(0) + '\u00a0\u20ac';
      if (insSpread && avgSpread > 0) insSpread.textContent = '\u00b1' + avgSpread + '%';
      if (insConf) insConf.textContent = confText || '87/100';

      // ── Insight cards : Opportunité détectée (Trust Engine) ──
      const insOpp = el('ins-opp');
      if (insOpp && arr.length > 0) {
        // Agréger les signaux trust engine sur tous les items
        const sigs = arr.map(function(it) { return _opportunitySignal(it); });
        const redCount    = sigs.filter(function(s) { return s.color === 'red'; }).length;
        const orangeCount = sigs.filter(function(s) { return s.color === 'orange'; }).length;
        const greenCount  = sigs.filter(function(s) { return s.color === 'green'; }).length;
        const avgTrust    = arr.reduce(function(acc, it) {
          return acc + Number((it.trust_report || {}).trust_score || it.score || 0);
        }, 0) / arr.length;

        let sigHtml;
        if (redCount / arr.length > 0.3) {
          sigHtml = '<span style="color:#ff3344;font-weight:700">\u26a0 Vigilance — offres suspectes d\u00e9tect\u00e9es</span>';
        } else if (orangeCount / arr.length > 0.5) {
          sigHtml = '<span style="color:#ffaa00;font-weight:700">\u2248 March\u00e9 variable — prix \u00e0 v\u00e9rifier</span>';
        } else if (greenCount / arr.length >= 0.6 && avgTrust >= 70) {
          sigHtml = '<span style="color:#00ff88;font-weight:700">\u2713 Prix coh\u00e9rents — sources fiables</span>';
        } else {
          sigHtml = '<span style="color:#ffd700;font-weight:700">\u2696 March\u00e9 \u00e9quilibr\u00e9</span>';
        }
        insOpp.innerHTML = sigHtml + '<div style="font-size:.7rem;color:#4a6070;margin-top:3px">Trust moyen\u00a0: ' + Math.round(avgTrust) + '/100</div>';
      }
    }

    // Inject fade animation for low-price analysis module (once)
    if (!document.getElementById('sb-low-css')) {
      const _s = document.createElement('style');
      _s.id = 'sb-low-css';
      _s.textContent = '@keyframes sbFadeUp{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}';
      document.head.appendChild(_s);
    }
    // ── P5 : Popup transparence sources ──────────────────────────────────────
    // Boutiques exclues : marketplaces non vérifiées, sources junk
    const _JUNK_SHOP_PATTERNS = ['ebay', 'vinted', 'leboncoin', 'occasion', 'used',
      'marketplace', 'aliexpress', 'wish', 'temu', 'shein', 'unknown', 'divers',
      'agrege', 'agrege fr', 'manual fr', 'source aggreg', 'generic'];
    function _isCredibleShop(shop) {
      if (!shop) return false;
      const sl = shop.toLowerCase();
      return !_JUNK_SHOP_PATTERNS.some(p => sl.indexOf(p) !== -1);
    }
    function _shopTrustBadge(shop, price) {
      // Boutiques premium connues
      const premiumShops = ['courir', 'foot locker', 'footlocker', 'jd sports', 'snipes',
        'zalando', 'nike', 'adidas', 'new balance', 'asics', 'puma', 'reebok', 'vans',
        'converse', 'intersport', 'sport 2000', 'sprinter', 'basket4ballers', 'size?',
        'offspring', 'end clothing', 'asos', 'zara', 'fnac', 'decathlon'];
      const sl = (shop || '').toLowerCase();
      if (premiumShops.some(p => sl.indexOf(p) !== -1))
        return '<span style="font-size:.6rem;padding:1px 5px;border-radius:4px;background:#00ff8822;color:#00ff88;border:1px solid #00ff8844;margin-left:4px">Premium</span>';
      return '<span style="font-size:.6rem;padding:1px 5px;border-radius:4px;background:#6366f122;color:#818cf8;border:1px solid #6366f144;margin-left:4px">Revendeur</span>';
    }
    function showSourcesPopup(it) {
      // Collecte des sources vérifiées : SerpAPI premium_rows uniquement
      const rawSources = Array.isArray(it.premium_sources) ? it.premium_sources : [];
      const sources = rawSources.filter(s => _isCredibleShop(s.shop) && Number(s.price) > 0);
      const brand = it.brand || '';
      const model = it.model || '';
      // Popup overlay
      let overlay = document.getElementById('sb-sources-overlay');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'sb-sources-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px';
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
        document.body.appendChild(overlay);
      }
      const srcCount = Number(it.source_count || 0);
      // Score unifié : trust_score prioritaire sur confidence_score
      const tr0      = it.trust_report || {};
      const uniScore = Number(tr0.trust_score || it.confidence_score || it.score || 0);
      const uniLabel = uniScore > 0 ? _trustLabel(uniScore) : (it.confidence_label || '');
      const uniCol   = uniScore > 0 ? _trustColor(uniScore) : 'orange';
      const uniP     = _tc(uniCol);
      const freshnessBadge = it.source_premium_used
        ? '<span style="font-size:.65rem;padding:2px 8px;border-radius:999px;background:#fbbf2422;color:#fbbf24;border:1px solid #fbbf2444">⭐ Source premium validée</span>'
        : '';
      let sourceRows = '';
      if (sources.length === 0) {
        sourceRows = '<p style="color:#6b7280;font-size:.85rem;padding:12px 0">Détail boutiques non disponible pour ce modèle.<br>Les données agrégées restent fiables.</p>';
      } else {
        // Tri par prix croissant
        const sorted = sources.slice().sort((a, b) => Number(a.price) - Number(b.price));
        sourceRows = sorted.map(s => {
          const priceStr = Number(s.price).toFixed(2).replace('.', ',') + '\u00a0\u20ac';
          const tier = _clientTier(s.shop);
          const logo = _shopLogo(s.shop);
          const tb   = _tierBadge(tier);
          const isVerified = (tier === 'official' || tier === 'major_retailer');
          const linkHtml = s.link
            ? `<a href="${s.link}" target="_blank" rel="noopener noreferrer" style="color:#6366f1;font-size:.68rem;margin-left:4px;text-decoration:none">↗</a>`
            : '';
          const priceCol = tier === 'suspect' ? '#ff6b6b' : tier === 'official' ? '#fbbf24' : '#00ff88';
          return `<div style="display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid #121c14;gap:8px">
            <div style="display:flex;align-items:center;gap:7px;min-width:0;flex:1;overflow:hidden">
              ${logo}
              <div style="display:flex;flex-direction:column;gap:2px;min-width:0">
                <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
                  <span style="font-size:.8rem;color:#d0e8d8;font-weight:600;word-break:break-word">${escAttr(s.shop)}</span>
                  ${isVerified ? '<span class="sb-verified-badge">✓ VÉRIFIÉ</span>' : ''}
                  ${linkHtml}
                </div>
                <div>${tb}</div>
              </div>
            </div>
            <span style="font-size:.92rem;font-weight:800;color:${priceCol};white-space:nowrap;flex-shrink:0">${priceStr}</span>
          </div>`;
        }).join('');
      }
      overlay.innerHTML = `<div style="background:#111;border:1px solid #2a2a2a;border-radius:16px;max-width:480px;width:100%;padding:24px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.8)">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:16px">
          <div>
            <h3 style="margin:0 0 4px;font-size:1rem;color:#f4f4f5">${brand} ${model}</h3>
            <p style="margin:0;font-size:.78rem;color:#6b7280">Sources de prix v\u00e9rifi\u00e9es</p>
          </div>
          <button onclick="document.getElementById('sb-sources-overlay').remove()" style="background:transparent;border:1px solid #333;color:#aaa;border-radius:8px;padding:4px 10px;cursor:pointer;font-size:.8rem">\u2715 Fermer</button>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
          <span style="font-size:.7rem;padding:3px 10px;border-radius:999px;background:#1a1a1a;color:#a1a1aa;border:1px solid #2a2a2a">${srcCount} sources analysées</span>
          <span style="font-size:.72rem;padding:3px 12px;border-radius:999px;background:${uniP.bg};color:${uniP.hex};border:1px solid ${uniP.border};font-weight:800">🔒 ${uniScore}/100 — ${uniLabel}</span>
          ${freshnessBadge}
        </div>
        <div style="font-size:.72rem;color:#6b7280;margin-bottom:12px;padding:8px;background:#0d0d0d;border-radius:8px;line-height:1.5">
          \uD83D\uDD12 Estimation march\u00e9 bas\u00e9e sur ${srcCount} source${srcCount > 1 ? 's' : ''} actives. Prix indicatifs — toujours v\u00e9rifier en boutique.
        </div>
        <div>${sourceRows}</div>
        ${renderTrustPanel(it.trust_report || null)}
        <p style="margin:16px 0 0;font-size:.68rem;color:#4b5563;text-align:center">Prix actualis\u00e9s 2 fois par jour \u2014 sneakerbot.shop</p>
      </div>`;
    }

    function renderSourcesBadge(it) {
      const sc = Number(it.source_count || 0);
      const conf = String(it.price_confidence || 'none');
      const hasPremium = Array.isArray(it.premium_sources) && it.premium_sources.filter(s => _isCredibleShop(s.shop) && Number(s.price) > 0).length > 0;
      // Trust Engine : rendre le badge cliquable aussi quand trust_report disponible
      const hasTrustReport = !!(it.trust_report);
      const cls = 'src-badge sb-' + conf;
      const label = sc === 0 ? '\u2014 sources' : (sc === 1 ? '1 source' : sc + ' sources');
      if (hasPremium || hasTrustReport) {
        return `<span class="${cls} sb-sources-clickable" data-count="${sc}" title="Cliquer pour voir le d\u00e9tail et l'indice de confiance" style="cursor:pointer">${label} \uD83D\uDD0D</span>`;
      }
      return `<span class="${cls}" data-count="${sc}" title="${label}">${label}</span>`;
    }
    function getSafePriceTriplet(item) {
      const safeMin = Number.isFinite(item && item.price_min) ? item.price_min : 0;
      const safeAvg = Number.isFinite(item && item.price_avg) ? item.price_avg : safeMin;
      const safeMax = Number.isFinite(item && item.price_max) ? item.price_max : safeAvg;
      return { safeMin, safeAvg, safeMax };
    }
    // ── Trust Engine UI — système unifié ─────────────────────────────────────
    // Palette cohérente
    const _TC = {
      green:  { hex:'#00ff88', bg:'#00ff8814', border:'#00ff8830' },
      orange: { hex:'#ffaa00', bg:'#ffaa0014', border:'#ffaa0030' },
      red:    { hex:'#ff3344', bg:'#ff334414', border:'#ff334430' },
    };
    function _tc(color) { return _TC[color] || _TC.orange; }

    // Label unifié — un seul wording dans toute l'UI
    function _trustLabel(score) {
      if (score >= 88) return 'Très fiable';
      if (score >= 72) return 'Fiable';
      if (score >= 55) return 'Acceptable';
      if (score >= 38) return 'Prudence';
      return 'Risque élevé';
    }
    function _trustColor(score) {
      if (score >= 72) return 'green';
      if (score >= 45) return 'orange';
      return 'red';
    }

    // Opportunity signal piloté par trust engine
    function _opportunitySignal(it) {
      const tr = it.trust_report || {};
      const score    = Number(tr.trust_score || 0);
      const excluded = Number(tr.excluded_anomalies_count || 0);
      const premium  = Number(tr.premium_sources_count || 0);
      const trusted  = Number(tr.trusted_sources_count || 0);
      const mState   = String(it.market_state || 'stable');
      const disp     = Number(it.dispersion || 0);
      const rec      = String(it.recommandation || '');
      const anomCls  = (it.anomaly_classification || {});
      const anomType = anomCls.type || 'NORMAL';
      const limited  = !!tr.data_limited;

      // ── Priorité 1 : alertes critiques ───────────────────────────────────────
      if (excluded > 0 && score < 60)
        return { label:'OFFRE SUSPECTE',   color:'red',    icon:'⚠' };
      if (anomType === 'SCRAPING_ERROR')
        return { label:'DONNÉE DOUTEUSE',  color:'red',    icon:'⚠' };

      // ── Priorité 2 : marché ───────────────────────────────────────────────────
      if (mState === 'unstable' || (mState === 'variable' && disp > 0.35))
        return { label:'MARCHÉ VOLATIL',   color:'orange', icon:'≋' };

      // ── Priorité 3 : promotions ───────────────────────────────────────────────
      if (anomType === 'PROMO_FLASH' || anomType === 'LAST_SIZE' || anomType === 'CLEARANCE')
        return { label:'PROMO POSSIBLE',   color:'orange', icon:'◈' };

      // ── Priorité 4 : prix cohérent validé par sources premium ─────────────────
      if (score >= 88 && premium >= 2 && trusted >= 4)
        return { label:'PRIX COHÉRENT',    color:'green',  icon:'✓' };

      // ── Priorité 5 : données insuffisantes (alerte orange seulement si score bas)
      // score >= 65 malgré limited → "Estimation prudente" neutre, pas d'alarme
      if (limited && score < 65)
        return { label:'DONNÉES LIMITÉES', color:'orange', icon:'○' };
      if (!limited && trusted <= 1)
        return { label:'SOURCES LIMITÉES', color:'orange', icon:'○' };

      // ── Priorité 6 : BON PRIX confirmé ───────────────────────────────────────
      if (rec.indexOf('BON PRIX') !== -1 || rec.indexOf('MEILLEUR') !== -1)
        return { label:'BON PRIX VÉRIFIÉ', color:'green',  icon:'✓' };

      // ── Défaut contextuel : pas un label générique ────────────────────────────
      if (score >= 88)
        return { label:'TRÈS FIABLE',      color:'green',  icon:'✓' };
      if (score >= 72)
        return { label:'PRIX VÉRIFIÉ',     color:'green',  icon:'·' };
      if (score >= 55)
        return { label:'ESTIMATION',       color:'orange', icon:'·' };
      return   { label:'À VÉRIFIER',       color:'orange', icon:'○' };
    }

    /**
     * renderTrustCell(it) — Badge TRUST XX/100 + tooltip + signal.
     * Enveloppé dans .sb-tt-wrap[data-mid] pour le tooltip hover.
     */
    function renderTrustCell(it) {
      const tr = it.trust_report || null;
      const rawScore = Number(it.score || 0);
      const mid = sneakerModelId(it.brand, it.model);

      if (!tr) {
        return `<div style="font-size:.78rem;font-weight:700;color:#6b7280">${rawScore}/100</div>`;
      }

      const score   = Number(tr.trust_score || rawScore);
      const col     = _trustColor(score);
      const lbl     = _trustLabel(score);
      const p       = _tc(col);
      const trusted  = Number(tr.trusted_sources_count || 0);
      const excluded = Number(tr.excluded_anomalies_count || 0);
      const premium  = Number(tr.premium_sources_count || 0);

      const sig  = _opportunitySignal(it);
      const sigP = _tc(sig.color);

      // .sb-tt-wrap carries data-mid for delegated tooltip
      return `<div class="sb-tt-wrap" data-mid="${escAttr(mid)}" style="display:flex;flex-direction:column;gap:4px;min-width:82px;cursor:default">
        <div style="display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:5px;background:${p.bg};border:1px solid ${p.border};width:fit-content">
          <span style="font-size:.58rem;color:${p.hex};font-weight:800;letter-spacing:.08em">TRUST</span>
          <span style="font-size:.88rem;font-weight:900;color:#fff;line-height:1">${score}</span>
          <span style="font-size:.58rem;color:#4a6070">/100</span>
        </div>
        <div style="font-size:.6rem;color:${p.hex};font-weight:700;letter-spacing:.05em">${lbl}</div>
        <div style="font-size:.57rem;color:#4a6070;line-height:1.4">
          ${trusted}\u00a0src${excluded > 0 ? `\u00a0·\u00a0<span style="color:#ffaa00">${excluded}\u00a0excl.</span>` : ''}${premium > 0 ? `\u00a0·\u00a0<span style="color:#fbbf24">${premium}\u00a0prem.</span>` : ''}
        </div>
        <div class="sb-trust-sig" style="display:inline-flex;align-items:center;gap:3px;padding:2px 6px;border-radius:3px;background:${sigP.bg};border:1px solid ${sigP.border};width:fit-content;margin-top:1px">
          <span style="font-size:.55rem;color:${sigP.hex};font-weight:800;letter-spacing:.05em">${sig.icon}\u00a0${sig.label}</span>
        </div>
      </div>`;
    }

    /**
     * renderTrustBadge(tr) : badge inline compact pour la ligne d'observation.
     */
    function renderTrustBadge(tr) {
      if (!tr) return '';
      const score = Number(tr.trust_score || 60);
      const col   = _trustColor(score);
      const lbl   = _trustLabel(score);
      const p     = _tc(col);
      const trusted  = Number(tr.trusted_sources_count || 0);
      const excluded = Number(tr.excluded_anomalies_count || 0);
      const premium  = Number(tr.premium_sources_count || 0);
      const parts = [`${trusted} src`];
      if (excluded > 0) parts.push(`${excluded} excl.`);
      if (premium  > 0) parts.push(`${premium} premium`);
      return `<span style="display:inline-flex;flex-direction:column;gap:1px;padding:3px 8px;border-radius:5px;font-family:monospace;background:${p.bg};border:1px solid ${p.border};vertical-align:middle;cursor:pointer" class="sb-sources-clickable" title="Voir détail Trust Engine">` +
        `<span style="font-size:.63rem;font-weight:900;color:${p.hex};letter-spacing:.07em">🔒 TRUST ${score}/100 — ${lbl}</span>` +
        `<span style="font-size:.58rem;color:#6b8096">${parts.join(' · ')}</span>` +
        `</span>`;
    }

    /**
     * renderTrustPanel(tr) — panneau popup Sources (détail complet).
     */
    function renderTrustPanel(tr) {
      if (!tr) return '';
      const score    = Number(tr.trust_score || 60);
      const col      = _trustColor(score);
      const lbl      = _trustLabel(score);
      const p        = _tc(col);
      const trusted  = Number(tr.trusted_sources_count || 0);
      const excluded = Number(tr.excluded_anomalies_count || 0);
      const premium  = Number(tr.premium_sources_count || 0);
      const shops    = Number(tr.distinct_shops_count || 0);
      const expl     = String(tr.explanation || '');
      const pnames   = Array.isArray(tr.premium_shop_names) ? tr.premium_shop_names : [];
      const limited  = !!tr.data_limited;
      const anomDets = Array.isArray(tr.anomaly_details) ? tr.anomaly_details : [];
      const barW     = Math.min(100, score);

      const pill = (lbl2, val, clr) =>
        `<div style="background:#0d1117;border:1px solid #1e2d3d;border-radius:5px;padding:6px 10px;text-align:center">` +
        `<div style="font-size:.58rem;color:#4a6070;text-transform:uppercase;letter-spacing:.08em;margin-bottom:2px">${lbl2}</div>` +
        `<div style="font-size:.9rem;font-weight:800;color:${clr || '#c8d8e8'}">${val}</div></div>`;

      let html = `<div style="margin-top:14px;padding:14px 16px;background:#06090e;border:1px solid ${p.border};border-radius:10px">`;

      // ── En-tête score
      html += `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;flex-wrap:wrap;gap:6px">`;
      html += `<div style="display:flex;flex-direction:column;gap:2px">`;
      html += `<span style="font-size:.62rem;color:#4a6070;text-transform:uppercase;letter-spacing:.1em;font-weight:700">Indice de Confiance Prix</span>`;
      html += `<span style="font-size:1rem;font-weight:900;color:${p.hex}">${score}/100 <span style="font-size:.72rem;font-weight:700">${lbl}</span></span>`;
      html += `</div>`;
      if (limited) html += `<span style="font-size:.6rem;padding:2px 8px;border-radius:999px;background:#ffaa0014;border:1px solid #ffaa0030;color:#ffaa00">Données limitées</span>`;
      html += `</div>`;

      // ── Barre score
      html += `<div style="height:3px;background:#1a2230;border-radius:2px;margin-bottom:12px;overflow:hidden">`;
      html += `<div style="height:100%;width:${barW}%;background:linear-gradient(90deg,${p.hex}88,${p.hex});border-radius:2px"></div>`;
      html += `</div>`;

      // ── Grille métriques
      html += `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px">`;
      html += pill('Fiables',  trusted,  trusted >= 3 ? '#00ff88' : '#ffaa00');
      html += pill('Exclues',  excluded, excluded === 0 ? '#00ff88' : '#ffaa00');
      html += pill('Premium',  premium,  premium >= 1 ? '#fbbf24' : '#6b7280');
      html += pill('Boutiques',shops,    shops >= 5   ? '#00ff88' : '#c8d8e8');
      html += `</div>`;

      // ── Sources premium nommées
      if (pnames.length > 0) {
        html += `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">`;
        pnames.forEach(n => {
          html += `<span style="font-size:.62rem;padding:2px 7px;border-radius:3px;background:#fbbf2414;border:1px solid #fbbf2430;color:#fbbf24">⭐ ${n}</span>`;
        });
        html += `</div>`;
      }

      // ── Explication
      if (expl) {
        html += `<div style="font-size:.68rem;color:#6b8096;line-height:1.55;padding:8px 10px;background:#080c10;border-radius:6px;border-left:2px solid ${p.hex}44">`;
        html += expl;
        html += `</div>`;
      }

      // ── Anomalies exclues (trace)
      if (anomDets.length > 0) {
        html += `<details style="margin-top:8px">`;
        html += `<summary style="font-size:.62rem;color:#4a6070;cursor:pointer;padding:4px 0;list-style:none">▸ ${anomDets.length} offre${anomDets.length > 1 ? 's' : ''} exclue${anomDets.length > 1 ? 's' : ''} de l'analyse</summary>`;
        html += `<div style="margin-top:6px;display:flex;flex-direction:column;gap:3px">`;
        anomDets.forEach(a => {
          const sev = a.severity === 'high' ? '#ff3344' : '#ffaa00';
          html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;background:#0d1117;border-radius:4px;border-left:2px solid ${sev}">`;
          html += `<span style="font-size:.62rem;color:#94a3b8">${a.shop || '—'}</span>`;
          html += `<span style="font-size:.62rem;color:#4a6070">${a.reason || ''}</span>`;
          html += `<span style="font-size:.65rem;font-weight:700;color:${sev}">${Number(a.price || 0).toFixed(0)} €</span>`;
          html += `</div>`;
        });
        html += `</div></details>`;
      }

      // ── Pourquoi ce score ? (breakdown) ──────────────────────────────────────
      const why = _whyScoreItems(tr);
      if (why.length > 0) {
        html += `<details style="margin-top:8px">`;
        html += `<summary style="font-size:.62rem;color:#4a6070;cursor:pointer;padding:4px 0;list-style:none;user-select:none">▸ Pourquoi ce score ?</summary>`;
        html += `<div style="margin-top:6px;padding:8px 10px;background:#06090e;border-radius:6px">`;
        html += `<div style="font-size:.58rem;color:#2a4a60;margin-bottom:6px;letter-spacing:.06em">DÉCOMPOSITION DU SCORE</div>`;
        why.forEach(function(item) {
          const [delta, label, sign] = item;
          const dc = sign === 'green' ? '#00ff88' : sign === 'red' ? '#ff4455' : '#4a6070';
          html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #0a1420">`;
          html += `<span style="font-size:.62rem;color:#6b8096">${label}</span>`;
          html += `<span style="font-size:.65rem;font-weight:800;color:${dc};min-width:30px;text-align:right">${delta}</span>`;
          html += `</div>`;
        });
        html += `<div style="display:flex;justify-content:space-between;align-items:center;padding-top:5px;margin-top:2px">`;
        html += `<span style="font-size:.62rem;color:#4a6070;font-weight:700">Score final</span>`;
        html += `<span style="font-size:.72rem;font-weight:900;color:${p.hex}">${score}/100</span>`;
        html += `</div></div></details>`;
      }

      html += `</div>`;
      return html;
    }

    // ──────────────────────────────────────────────────────────────────────────
    // TRUST ENGINE PREMIUM UI v2.1
    // Tooltip · Logos · WhyScore · Sparkline
    // ──────────────────────────────────────────────────────────────────────────

    // ── "Pourquoi ce score ?" breakdown ──────────────────────────────────────
    function _whyScoreItems(tr) {
      if (!tr) return [];
      const trusted = Number(tr.trusted_sources_count || 0);
      const excl    = Number(tr.excluded_anomalies_count || 0);
      const premium = Number(tr.premium_sources_count || 0);
      const shops   = Number(tr.distinct_shops_count || 0);
      const limited = !!tr.data_limited;
      const items   = [];

      items.push(['+40', 'Base', 'neutral']);

      const srcPts = trusted >= 10 ? 25 : trusted >= 5 ? 18 : trusted >= 3 ? 12 : trusted >= 2 ? 6 : trusted >= 1 ? 2 : 0;
      if (srcPts) items.push(['+' + srcPts, trusted + ' source' + (trusted > 1 ? 's' : '') + ' fiable' + (trusted > 1 ? 's' : ''), 'green']);

      const premPts = premium >= 3 ? 20 : premium >= 2 ? 14 : premium >= 1 ? 8 : 0;
      if (premPts) items.push(['+' + premPts, premium + ' source' + (premium > 1 ? 's' : '') + ' premium', 'green']);

      const divPts = shops >= 8 ? 10 : shops >= 5 ? 6 : shops >= 3 ? 3 : 0;
      if (divPts) items.push(['+' + divPts, shops + ' boutiques distinctes', 'green']);

      const total = trusted + excl;
      if (total > 0) {
        const ratio = excl / total;
        const pen = ratio > 0.50 ? -15 : ratio > 0.30 ? -8 : ratio > 0.15 ? -3 : excl > 0 ? -1 : 0;
        if (pen) items.push([String(pen), excl + ' anomalie' + (excl > 1 ? 's' : '') + ' exclue' + (excl > 1 ? 's' : ''), 'red']);
      }
      if (limited) items.push(['-5', 'Données limitées', 'red']);

      return items;
    }

    // ── Shop logo map [initials, bg, fg] ─────────────────────────────────────
    const _SL = {
      'nike': ['NK','#111','#f5f5f5'], 'adidas': ['AD','#050505','#fff'],
      'zalando': ['ZL','#ff6900','#fff'], 'footlocker': ['FL','#e30613','#fff'],
      'foot locker': ['FL','#e30613','#fff'], 'jd sports': ['JD','#111','#ffd700'],
      'new balance': ['NB','#cc0000','#fff'], 'puma': ['PU','#162444','#fff'],
      'converse': ['CO','#232323','#fff'], 'vans': ['VA','#e32636','#fff'],
      'asics': ['AS','#003da5','#fff'], 'reebok': ['RB','#0b3d91','#fff'],
      'salomon': ['SA','#c00','#fff'], 'on ': ['ON','#333','#fff'],
      'decathlon': ['DC','#3643ba','#fff'], 'intersport': ['IS','#f58220','#fff'],
      'courir': ['CR','#d32f2f','#fff'], 'snipes': ['SN','#111','#fff'],
      'fnac': ['FN','#f0a500','#000'], 'asos': ['AO','#2d2d2d','#fff'],
      'amazon': ['AM','#ff9900','#232f3e'], 'stockx': ['SX','#00873b','#fff'],
      'farfetch': ['FF','#444','#fff'], 'solebox': ['SB','#111','#00ff88'],
      'wethenew': ['WN','#1a1a2e','#fff'], 'basket4ballers': ['B4','#0066cc','#fff'],
      'sport 2000': ['S2','#ffd600','#000'], 'sarenza': ['SZ','#e91e63','#fff'],
      'galeries lafayette': ['GL','#b8860b','#fff'], 'mytheresa': ['MT','#111','#fff'],
      'manfield': ['MF','#8b0000','#fff'], 'spartoo': ['SP','#ff4500','#fff'],
      'aboutyou': ['AY','#6200ea','#fff'], 'rakuten': ['RK','#bf0000','#fff'],
      'mac douglas': ['MD','#2c1810','#fff'],
    };

    function _shopLogo(shopName) {
      const n = (shopName || '').toLowerCase().trim();
      for (const key of Object.keys(_SL)) {
        if (n.indexOf(key) !== -1) {
          const [ini, bg, fg] = _SL[key];
          return `<span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:${bg};color:${fg};font-size:.58rem;font-weight:900;letter-spacing:.01em;flex-shrink:0;font-family:'SF Mono',monospace">${ini}</span>`;
        }
      }
      const ini = n.replace(/[^a-z]/g, '').substring(0, 2).toUpperCase() || '??';
      return `<span style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;background:#0d1a22;color:#4a6070;font-size:.58rem;font-weight:900;flex-shrink:0;font-family:'SF Mono',monospace">${ini}</span>`;
    }

    function _clientTier(shopName) {
      const n = (shopName || '').toLowerCase().trim();
      const suspect = ['resell','babunkers','madonnina','pikastore','solem8te','beebs','secondstep','hylton'];
      const official = ['nike officiel','nike fr','adidas.fr','adidas fr','new balance','puma.com','asics.com','vans.com','converse.com','on running','on.com','reebok.com','salomon.com','mac douglas'];
      const major = ['zalando','footlocker','foot locker','jd sports','intersport','courir','sarenza','asos','fnac','decathlon','galeries lafayette','la redoute','spartoo','snipes','sport 2000','aboutyou','mytheresa','manfield','sprinter'];
      const specialist = ['solebox','wethenew','basket4ballers','asphaltgold','footdistrict','pro:direct','sneak boutik','skatedeluxe','kickscrew','size factory','offshoes','overkill','bstn'];
      if (suspect.some(s => n.indexOf(s) !== -1)) return 'suspect';
      if (official.some(s => n.indexOf(s) !== -1)) return 'official';
      if (major.some(s => n.indexOf(s) !== -1)) return 'major_retailer';
      if (specialist.some(s => n.indexOf(s) !== -1)) return 'specialist';
      if (['amazon','stockx','farfetch','rakuten','leboncoin','ebay','vestiaire'].some(s => n.indexOf(s) !== -1)) return 'marketplace';
      return 'unknown';
    }

    function _tierBadge(tier) {
      const map = {
        official:       ['⭐ OFFICIEL',  '#fbbf2414','#fbbf24','#fbbf2440'],
        major_retailer: ['✓ VÉRIFIÉ',   '#00ff8812','#34d399','#00ff8835'],
        specialist:     ['◆ SPECIALIST','#6366f114','#818cf8','#6366f135'],
        marketplace:    ['◎ MARCHÉ',    '#7dd3fc12','#7dd3fc','#7dd3fc30'],
        unknown:        ['○ NON VÉRIFIÉ','#6b728010','#6b7280','#6b728025'],
        suspect:        ['⚠ SUSPECT',   '#ff334412','#ff6b6b','#ff334435'],
      };
      const [lbl, bg, color, border] = map[tier] || map.unknown;
      return `<span style="font-size:.52rem;padding:1px 5px;border-radius:3px;background:${bg};color:${color};border:1px solid ${border};font-weight:700;letter-spacing:.04em;white-space:nowrap">${lbl}</span>`;
    }

    // ── Trust tooltip HTML — Bloomberg glassmorphism premium ─────────────────
    function _buildTrustTtHtml(tr) {
      if (!tr) return '';
      const score   = Number(tr.trust_score || 0);
      const lbl     = _trustLabel(score);
      const col     = _trustColor(score);
      const p       = _tc(col);
      const trusted = Number(tr.trusted_sources_count || 0);
      const excl    = Number(tr.excluded_anomalies_count || 0);
      const premium = Number(tr.premium_sources_count || 0);
      const shops   = Number(tr.distinct_shops_count || 0);
      const limited = !!tr.data_limited;
      const expl    = String(tr.explanation || '');
      // Score bar fill
      const barPct  = Math.min(100, score);
      const barClr  = score >= 88 ? '#00ff88' : score >= 72 ? '#4ade80' : score >= 55 ? '#fbbf24' : score >= 38 ? '#f97316' : '#f87171';

      function row(icon, label, val, vc) {
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)">` +
          `<span style="font-size:.58rem;color:rgba(148,163,184,.7);display:flex;align-items:center;gap:4px"><span style="opacity:.6">${icon}</span>${label}</span>` +
          `<span style="font-size:.62rem;font-weight:800;color:${vc};letter-spacing:.02em">${val}</span></div>`;
      }

      return `<div style="position:relative">` +
        // Header: score big + label
        `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">` +
          `<div style="display:flex;align-items:baseline;gap:4px">` +
            `<span style="font-size:1.3rem;font-weight:900;color:${p.hex};letter-spacing:-.04em;font-variant-numeric:tabular-nums">${score}</span>` +
            `<span style="font-size:.55rem;color:${p.hex};opacity:.6;font-weight:600">/100</span>` +
          `</div>` +
          `<span style="font-size:.65rem;font-weight:800;color:${p.hex};letter-spacing:.04em;text-transform:uppercase;padding:2px 8px;border-radius:4px;background:${p.hex}14;border:1px solid ${p.hex}28">${lbl}</span>` +
        `</div>` +
        // Score bar
        `<div style="height:3px;background:rgba(255,255,255,.06);border-radius:3px;margin-bottom:10px;overflow:hidden">` +
          `<div style="height:100%;width:${barPct}%;background:linear-gradient(90deg,${barClr}88,${barClr});border-radius:3px;transition:width .6s cubic-bezier(.22,1,.36,1)"></div>` +
        `</div>` +
        // Metrics
        row('⬡', 'Sources fiables',   trusted, trusted >= 3 ? '#4ade80' : '#fb923c') +
        row('★', 'Sources premium',   premium, premium >= 1 ? '#fbbf24' : 'rgba(107,114,128,.8)') +
        row('✗', 'Anomalies exclues', excl,    excl === 0    ? '#4ade80' : '#f87171') +
        row('◉', 'Boutiques',         shops,   shops >= 5   ? '#4ade80' : 'rgba(203,213,225,.7)') +
        // Explanation snippet
        (expl ? `<div style="margin-top:8px;font-size:.55rem;color:rgba(100,130,148,.85);line-height:1.55;padding:6px 8px;background:rgba(0,0,0,.35);border-radius:6px;border-left:2px solid ${p.hex}40">${expl.substring(0, 120)}${expl.length > 120 ? '…' : ''}</div>` : '') +
        // Footer CTA
        `<div style="margin-top:9px;font-size:.52rem;color:rgba(30,58,74,.9);text-align:center;letter-spacing:.03em">↙ CLIQUER POUR DÉTAIL SOURCES</div>` +
      `</div>`;
    }

    // ── Global tooltip singleton ──────────────────────────────────────────────
    let _sbTT = null;
    let _sbTtActive = null;

    function _getTT() {
      if (_sbTT) return _sbTT;
      _sbTT = document.createElement('div');
      _sbTT.id = 'sb-global-tt';
      document.body.appendChild(_sbTT);
      return _sbTT;
    }

    function _sbShowTt(anchor, html) {
      const tt = _getTT();
      tt.innerHTML = html;
      tt.style.display = 'block';
      tt.classList.remove('sb-tt-visible');
      const rect = anchor.getBoundingClientRect();
      const ttW = 245, margin = 10;
      let left = rect.right + margin;
      // Overflow right → flip left
      if (left + ttW > window.innerWidth - margin) left = rect.left - ttW - margin;
      // Still overflow left → center under
      if (left < margin) left = Math.max(margin, rect.left + rect.width / 2 - ttW / 2);
      left = Math.min(left, window.innerWidth - ttW - margin);
      let top = rect.top;
      const ttH = 240; // estimate
      if (top + ttH > window.innerHeight - margin) top = window.innerHeight - ttH - margin;
      top = Math.max(margin, top);
      tt.style.left = left + 'px';
      tt.style.top  = top  + 'px';
      requestAnimationFrame(function() { tt.classList.add('sb-tt-visible'); });
    }

    function _sbHideTt() {
      if (_sbTT) _sbTT.classList.remove('sb-tt-visible');
    }

    // ── Sparkline engine ──────────────────────────────────────────────────────
    const _sparkCache = new Map(); // mid → svg string
    const _sparkMap   = new Map(); // sparkId → mid
    let   _sparkIdx   = 0;
    let   _sparkObs   = null;

    let _sparkUid = 0;
    function _sparkSvg(points, W, H) {
      W = W || 72; H = H || 24;
      const prices = (points || []).map(function(p) { return Number(p.price_avg || p.price || 0); }).filter(function(p) { return p > 0; });
      if (prices.length < 2) {
        return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><line x1="4" y1="${H/2}" x2="${W-4}" y2="${H/2}" stroke="#1e3040" stroke-width="1.5" stroke-dasharray="3,3" stroke-linecap="round"/></svg>`;
      }
      const uid    = 'spk' + (++_sparkUid);
      const minP   = Math.min.apply(null, prices), maxP = Math.max.apply(null, prices);
      const range  = (maxP - minP) || 1;
      const pad    = 3;
      const coords = prices.map(function(p, i) {
        return {
          x: pad + (i / (prices.length - 1)) * (W - 2 * pad),
          y: H - pad - ((p - minP) / range) * (H - 2 * pad)
        };
      });
      // Smooth polyline via SVG path with cubic bezier
      let d = 'M' + coords[0].x.toFixed(1) + ',' + coords[0].y.toFixed(1);
      for (let i = 1; i < coords.length; i++) {
        const c0 = coords[i - 1], c1 = coords[i];
        const cx = ((c0.x + c1.x) / 2).toFixed(1);
        d += ' C' + cx + ',' + c0.y.toFixed(1) + ' ' + cx + ',' + c1.y.toFixed(1) + ' ' + c1.x.toFixed(1) + ',' + c1.y.toFixed(1);
      }
      const trend   = prices[prices.length - 1] - prices[0];
      const c0hex   = trend < -0.5 ? '#00ff88' : trend > 0.5 ? '#ff5566' : '#60a5fa';
      const c1hex   = trend < -0.5 ? '#00cc55' : trend > 0.5 ? '#cc2233' : '#2563eb';
      const glowClr = trend < -0.5 ? 'rgba(0,255,136,0.45)' : trend > 0.5 ? 'rgba(255,85,102,0.45)' : 'rgba(96,165,250,0.35)';
      const lastX   = coords[coords.length - 1].x.toFixed(1);
      const lastY   = coords[coords.length - 1].y.toFixed(1);
      // Area fill path (under curve → close to bottom)
      const areaD   = d + ' L' + (W - pad).toFixed(1) + ',' + (H - pad + 1) + ' L' + pad + ',' + (H - pad + 1) + ' Z';
      // Path length approximation for stroke-dasharray animation
      const pathLen = Math.round(W * 1.6);
      return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" overflow="visible">` +
        `<defs>` +
          `<linearGradient id="${uid}g" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="${c1hex}"/><stop offset="100%" stop-color="${c0hex}"/></linearGradient>` +
          `<linearGradient id="${uid}a" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${c0hex}" stop-opacity=".18"/><stop offset="100%" stop-color="${c0hex}" stop-opacity="0"/></linearGradient>` +
          `<filter id="${uid}f" x="-20%" y="-60%" width="140%" height="220%">` +
            `<feGaussianBlur stdDeviation="2.2" result="blur"/>` +
            `<feFlood flood-color="${glowClr}" result="color"/>` +
            `<feComposite in="color" in2="blur" operator="in" result="glow"/>` +
            `<feMerge><feMergeNode in="glow"/><feMergeNode in="SourceGraphic"/></feMerge>` +
          `</filter>` +
          `<clipPath id="${uid}cp"><rect x="0" y="0" width="${W}" height="${H}"/></clipPath>` +
        `</defs>` +
        `<path d="${areaD}" fill="url(#${uid}a)" clip-path="url(#${uid}cp)" stroke="none"/>` +
        `<path d="${d}" fill="none" stroke="url(#${uid}g)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" filter="url(#${uid}f)" ` +
          `stroke-dasharray="${pathLen}" stroke-dashoffset="${pathLen}" clip-path="url(#${uid}cp)">` +
          `<animate attributeName="stroke-dashoffset" from="${pathLen}" to="0" dur="0.7s" fill="freeze" calcMode="spline" keyTimes="0;1" keySplines="0.25,0.1,0.25,1"/>` +
        `</path>` +
        `<circle cx="${lastX}" cy="${lastY}" r="2.4" fill="${c0hex}" filter="url(#${uid}f)" opacity="0">` +
          `<animate attributeName="opacity" from="0" to="1" begin="0.55s" dur="0.2s" fill="freeze"/>` +
        `</circle>` +
        `<circle cx="${lastX}" cy="${lastY}" r="2.4" fill="${c0hex}" opacity="0">` +
          `<animate attributeName="opacity" from="0" to="1" begin="0.55s" dur="0.2s" fill="freeze"/>` +
          `<animate attributeName="r" from="2.4" to="5" dur="1.2s" begin="0.75s" repeatCount="indefinite" calcMode="spline" keyTimes="0;0.5;1" keySplines="0.4,0,0.6,1;0.4,0,0.6,1"/>` +
          `<animate attributeName="opacity" from="1" to="0" dur="1.2s" begin="0.75s" repeatCount="indefinite" calcMode="spline" keyTimes="0;0.5;1" keySplines="0.4,0,0.6,1;0.4,0,0.6,1"/>` +
        `</circle>` +
      `</svg>`;
    }

    function _initSparklines() {
      if (_sparkObs) { _sparkObs.disconnect(); _sparkObs = null; }
      const phs = document.querySelectorAll('.sb-spark-ph[id]');
      if (!phs.length) return;

      _sparkObs = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
          if (!entry.isIntersecting) return;
          const el = entry.target;
          const id = el.id;
          const mid = _sparkMap.get(id);
          if (!mid) return;
          // Already in cache — apply immediately
          if (_sparkCache.has(mid)) {
            el.innerHTML = _sparkCache.get(mid);
            _sparkObs && _sparkObs.unobserve(el);
            return;
          }
          // Mark as loading to prevent duplicate fetches
          _sparkCache.set(mid, ''); // placeholder
          _sparkObs && _sparkObs.unobserve(el);
          el.innerHTML = '<div class="sb-spark-loading"></div>';
          fetchWithTimeout('/api/sneakers/' + encodeURIComponent(mid) + '/history?days=30', 6000)
            .then(function(r) { return r.ok ? r.json() : null; })
            .then(function(d) {
              const svg = _sparkSvg(d && d.history ? d.history : []);
              _sparkCache.set(mid, svg);
              // Update all placeholders with this mid
              _sparkMap.forEach(function(m, sid) {
                if (m === mid) {
                  const el2 = document.getElementById(sid);
                  if (el2) el2.innerHTML = svg;
                }
              });
            })
            .catch(function() { _sparkCache.set(mid, ''); });
        });
      }, { rootMargin: '80px', threshold: 0 });

      phs.forEach(function(ph) { _sparkObs.observe(ph); });
    }

    // ── V2 confidence badge helpers ───────────────────────────────────────────
    function v2ConfBadge(conf) {
      if (!conf) return '';
      const map = {
        strong: { bg: '#0f2f1f', color: '#4dff9f', label: 'v2 ✓ strong' },
        medium: { bg: '#2e260d', color: '#ffd66f', label: 'v2 ~ medium' },
        low:    { bg: '#3b1111', color: '#fda4af', label: 'v2 low' },
      };
      const s = map[conf] || map.low;
      return `<span style="display:inline-block;margin-left:5px;padding:2px 7px;border-radius:999px;font-size:.68rem;font-weight:800;background:${s.bg};color:${s.color}">${s.label}</span>`;
    }
    function v2MedianBadge(median) {
      if (median == null) return '';
      return `<span style="display:block;font-size:.68rem;color:#94a3b8;margin-top:2px">médiane : ${Number(median).toFixed(2)}\u00a0€</span>`;
    }
    // ── Module premium : Analyse de l'offre basse + badge anomaly P4 ────────
    function renderLowPriceAnalysis(minVal, avgVal, srcCount, dispersion, marketState, v2Conf, anomalyClassification) {
      if (!Number.isFinite(minVal) || !Number.isFinite(avgVal) || avgVal <= 0 || minVal <= 0) return '';
      const ratio = minVal / avgVal;
      if (ratio > 0.65) return '';

      // ── Score fiabilité : sources (règle principale) ──────────────────────
      const disp = Number.isFinite(dispersion) ? dispersion : 1;
      let relLabel = '', relColor = '', relBg = '', relBorder = '';
      if (srcCount >= 18 && disp <= 2.0) {
        relLabel = '\u{1F7E2} Tr\u00e8s fiable';    relColor = '#86efac'; relBg = '#071a0c'; relBorder = 'rgba(34,197,94,0.3)';
      } else if (srcCount >= 8) {
        relLabel = '\u{1F7E1} Fiable';              relColor = '#fde68a'; relBg = '#1a1400'; relBorder = 'rgba(234,179,8,0.3)';
      } else if (srcCount >= 3) {
        relLabel = '\u{1F7E0} Prudence';            relColor = '#fb923c'; relBg = '#1a0900'; relBorder = 'rgba(249,115,22,0.3)';
      } else {
        relLabel = '\u{1F534} Risque \u00e9lev\u00e9'; relColor = '#fca5a5'; relBg = '#1f0707'; relBorder = 'rgba(239,68,68,0.3)';
      }

      const delta = (100 - Math.round(ratio * 100));

      // ── Badge anomaly intelligent (P4) ───────────────────────────────────
      const cls = anomalyClassification || {};
      const anomType = cls.type || 'UNKNOWN';
      const anomBadge = cls.badge || '';
      const anomColor = cls.color || '#f59e0b';
      // Messages contextuels par type
      const anomalyMessages = {
        'PROMO_FLASH':    ['• Promotion temporaire d\u00e9tect\u00e9e', '• V\u00e9rification disponibilit\u00e9 recommand\u00e9e'],
        'LAST_SIZE':      ['• Derni\u00e8res tailles probables', '• Stock limit\u00e9 en boutique'],
        'CLEARANCE':      ['• Op\u00e9ration de d\u00e9stockage possible', '• Offre sur tailles moins demand\u00e9es'],
        'KIDS_VARIANT':   ['• Variante enfant/junior d\u00e9tect\u00e9e', '• V\u00e9rifier la pointure avant commande'],
        'USED_POSSIBLE':  ['• Produit d\u2019occasion possible', '• V\u00e9rifier l\u2019\u00e9tat du produit'],
        'SCRAPING_ERROR': ['• Donn\u00e9e potentiellement incorrecte', '• Recharger dans quelques heures'],
        'UNKNOWN':        ['• Derni\u00e8res tailles possibles', '• Promotion temporaire possible', '• Offre atypique possible'],
        'NORMAL':         []
      };
      const msgs = anomalyMessages[anomType] || anomalyMessages['UNKNOWN'];
      const anomBadgeHtml = (anomBadge && anomType !== 'NORMAL')
        ? `<div style="display:inline-block;margin-bottom:6px;padding:3px 10px;border-radius:999px;font-size:.64rem;font-weight:800;background:${anomColor}22;color:${anomColor};border:1px solid ${anomColor}55">${anomBadge}</div>`
        : '';
      const msgLines = msgs.map(m => `<div style="font-size:clamp(.62rem,1.8vw,.7rem);color:#9fffd1;line-height:1.35">${m}</div>`).join('');

      return `<div class="sb-low-offer-card" style="margin-top:6px;padding:8px 10px;background:#070f09;border:1px solid rgba(0,255,136,0.14);border-radius:10px;box-shadow:0 2px 16px rgba(0,0,0,0.5);animation:sbFadeUp .3s ease;min-width:0;width:100%;max-width:100%;white-space:normal;word-break:break-word;overflow:visible;overflow-wrap:anywhere">` +
        `<div style="display:flex;flex-wrap:wrap;align-items:flex-start;gap:4px 6px;margin-bottom:5px;min-width:0">` +
          `<span style="font-size:.58rem;font-weight:800;color:#00d876;letter-spacing:.05em;text-transform:uppercase;word-break:break-word;line-height:1.35">\uD83D\uDD0E\u00a0Analyse de l\u2019offre basse</span>` +
          `<span style="font-size:.58rem;color:#4b5563;margin-left:auto;white-space:normal;word-break:break-word;line-height:1.35">\u2212${delta}% vs moy.</span>` +
        `</div>` +
        anomBadgeHtml +
        `<div style="display:flex;flex-direction:column;align-items:flex-start;gap:4px;line-height:1.4;margin-bottom:6px;max-width:100%;overflow:visible;white-space:normal;word-break:break-word;overflow-wrap:anywhere">` +
          msgLines +
          (msgs.length === 0 || anomType === 'UNKNOWN' ? `<div style="font-size:clamp(.62rem,1.8vw,.7rem);color:#cbd5e1;line-height:1.35">\u2022 V\u00e9rification recommand\u00e9e</div>` : '') +
        `</div>` +
        `<div title="Score bas\u00e9 sur sources + coh\u00e9rence march\u00e9 + dispersion" style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:.58rem;font-weight:700;background:${relBg};color:${relColor};border:1px solid ${relBorder};white-space:normal;line-height:1.35;cursor:help">${relLabel}</div>` +
      `</div>`;
    }

    function renderRows(arr, relaxed=false) {
      if (!arr.length) {
        if (refreshModeActive && Array.isArray(lastRenderedItems) && lastRenderedItems.length) {
          console.info('[UI] cached data preserved');
          return;
        }
        rows.innerHTML = '<tr><td colspan="11" style="color:#9ca3af">Aucune donn\u00e9e pour ce filtre. S\u00e9lectionnez un autre mod\u00e8le.</td></tr>';
        return;
      }
      // Reset sparkline index for this render cycle
      _sparkIdx = 0;
      _sparkMap.clear();
      lastRenderedItems = arr.slice();
      const note = '';
      rows.innerHTML = note + arr.map(it => {
        const c = it.credibility === 'high' ? 'h' : (it.credibility === 'medium' ? 'm' : 'l');
        const score = Number(it.score || 0);
        const reason = it.excluded ? ` (exclu: ${it.exclusion_reason || 'raison_inconnue'})` : '';
        const gBadge = String(it.google_badge || 'none');
        const gDev = (it.google_deviation_pct == null) ? null : Number(it.google_deviation_pct);
        let gCell = '—';
        if (gBadge === 'ok') {
          gCell = '🟢 Google ✅';
        } else if (gBadge === 'warn') {
          gCell = '🟡 Google ⚠️';
        } else if (gBadge === 'bad') {
          gCell = '🔴 Google ❌';
        }
        const gHint = gDev == null ? '' : ' <span style="color:#6b7280;font-size:.75rem;">(~' + gDev.toFixed(1) + '%)</span>';
        const mid = sneakerModelId(it.brand, it.model);
        const rec = String(it.recommandation || '👍 BON PRIX');
        const pos = String(it.position_client || 'Milieu de gamme');
        const recCls = recBadgeClass(rec);
        const posCls = posBadgeClass(pos);
        const safePrices = getSafePriceTriplet(it);
        const marketState = String(it.market_state || 'stable');
        const marketDisp = Number(it.dispersion || 0);
        let marketText = 'Marché stable';
        if (marketState === 'unstable') {
          marketText = 'Marché instable – forte variation de prix';
        } else if (marketState === 'variable') {
          marketText = 'Prix variables selon les vendeurs';
        }
        const marketObservation = `<div class="market-observation ${escAttr(marketState)}">${marketText} (${
          Number.isFinite(marketDisp) ? (marketDisp * 100).toFixed(1) : '0.0'
        }%)</div>`;
        const sourcesCount = Number(it.source_count || 0);
        // v2: afficher nb outliers supprimés si dispo
        const v2note = it.v2_enabled
          ? ` · <span style="color:#6366f1;font-size:.68rem" title="Données filtrées par pipeline v2 (outliers supprimés)">⚡ v2</span>`
          : '';
        const premiumBadge = it.source_premium_used
          ? ' · <span style="color:#fbbf24;font-size:.68rem" title="Validation SerpAPI Google Shopping FR">⭐ Source premium utilisée</span>'
          : '';
        const reliableBadge = it.reliable_range_used
          ? ' · <span style="color:#22c55e;font-size:.68rem" title="Range fiable quartiles/médiane">Prix marché fiable</span>'
          : '';
        const filterHint = `<span class="cmp-filter-note">Analyse basée sur ${Number.isFinite(sourcesCount) ? sourcesCount : 0} sources (valeurs extrêmes filtrées)${v2note}${premiumBadge}${reliableBadge}</span>`;
        const waMin = Number(it.price_min);
        const waMsg = encodeURIComponent('🔥 ' + it.brand + ' ' + it.model + ' dès ' + (Number.isFinite(waMin) ? waMin : safePrices.safeAvg).toFixed(2).replace('.', ',') + ' € — sneakerbot.shop');
        const waLink = 'https://wa.me/' + WA_NUMBER + '?text=' + waMsg;
        const adminRawTooltip = (IS_ADMIN && it.reliable_range_used && Number.isFinite(Number(it.raw_price_min)) && Number.isFinite(Number(it.raw_price_max)))
          ? ` title="Brut: ${Number(it.raw_price_min).toFixed(2)}€ - ${Number(it.raw_price_max).toFixed(2)}€"`
          : '';
        const minAnalysis = renderLowPriceAnalysis(safePrices.safeMin, safePrices.safeAvg, sourcesCount, marketDisp, marketState, it.v2_confidence, it.anomaly_classification);
        const priceCells = `<td class="cmp-price"${minAnalysis ? ' style="vertical-align:top"' : ''}><span class="cmp-price-inner"${adminRawTooltip}>${safePrices.safeMin.toFixed(2)}${String.fromCharCode(0xA0)}€</span>${minAnalysis}</td>
          <td class="cmp-price"><span class="cmp-price-inner">${safePrices.safeAvg.toFixed(2)}${String.fromCharCode(0xA0)}€${it.v2_median != null ? v2MedianBadge(it.v2_median) : ''}</span></td>
          <td class="cmp-price"><span class="cmp-price-inner"${adminRawTooltip}>${safePrices.safeMax.toFixed(2)}${String.fromCharCode(0xA0)}€</span></td>`;
        // P5 : indice popup sources pour clic délégué
        const srcBadgeId = 'sb-src-' + mid;
        // Sparkline : ID unique, lazy-loaded via IntersectionObserver
        const hasHist = !!(it.has_history || Number(it.price_count || 0) > 2);
        const sparkId = 'sb-sp-' + (_sparkIdx++);
        _sparkMap.set(sparkId, mid);
        const sparkHtml = hasHist
          ? `<span class="sb-spark-ph" id="${escAttr(sparkId)}" title="Historique 30j"></span>`
          : '';
        return `<tr>
          <td style="vertical-align:top">${it.brand}\u00a0${it.model}${sparkHtml}</td>
          ${priceCells}
          <td style="vertical-align:top;padding:8px 6px">${renderTrustCell(it)}</td>
          <td><span class="pill ${c}">${it.credibility}</span>${it.v2_confidence ? v2ConfBadge(it.v2_confidence) : ''}<span style="color:#6b7280;font-size:.75rem;">${reason}</span></td>
          <td>${gCell}${gHint}</td>
          <td><span class="${recCls}" title="${escAttr(rec)}">${escAttr(rec)}</span></td>
          <td><span class="${posCls}" title="${escAttr(pos)}">${escAttr(pos)}</span></td>
          <td id="${escAttr(srcBadgeId)}">${renderSourcesBadge(it)}</td>
          <td style="white-space:nowrap">
            <a href="${waLink}" target="_blank" rel="noopener" class="btn-cmp-wa" title="Partager sur WhatsApp">💬 WA</a>
            ${(Number(it.price_count || 0) > 1 || it.has_history) ? `<button type="button" class="btn-cmp-hist" data-mid="${escAttr(mid)}" title="Historique ~30 j.">Historique</button>` : ''}
          </td>
        </tr>
        <tr class="cmp-observation-row">
          <td colspan="11">${marketObservation}${filterHint}</td>
        </tr>`;
      }).join('');
      // Init lazy sparklines after DOM update
      requestAnimationFrame(_initSparklines);
    }
    if (rows) {
      // ── Tooltip hover delegation ────────────────────────────────────────────
      let _sbTtLast = null;
      rows.addEventListener('mouseover', function(ev) {
        const trigger = ev.target.closest && ev.target.closest('.sb-tt-wrap');
        if (trigger === _sbTtLast) return;
        _sbTtLast = trigger;
        if (trigger) {
          const mid = trigger.getAttribute('data-mid');
          const item = Array.isArray(lastRenderedItems) ? lastRenderedItems.find(function(it) { return sneakerModelId(it.brand, it.model) === mid; }) : null;
          if (item && item.trust_report) _sbShowTt(trigger, _buildTrustTtHtml(item.trust_report));
        } else {
          _sbHideTt();
        }
      });
      rows.addEventListener('mouseleave', function() { _sbTtLast = null; _sbHideTt(); });

      // ── Touch: tap trust badge → open sources popup (no tooltip on mobile) ──
      rows.addEventListener('touchstart', function(ev) {
        const trigger = ev.target.closest && ev.target.closest('.sb-tt-wrap');
        if (!trigger) return;
        // Prevent ghost mouse event
        ev.preventDefault();
        // Hide tooltip (no hover on mobile)
        _sbHideTt();
        // Open sources popup instead
        const mid = trigger.getAttribute('data-mid');
        const item = Array.isArray(lastRenderedItems)
          ? lastRenderedItems.find(function(it) { return sneakerModelId(it.brand, it.model) === mid; })
          : null;
        if (item) showSourcesPopup(item);
      }, { passive: false });

      rows.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button.btn-cmp-hist');
        if (btn && rows.contains(btn)) {
          ev.preventDefault();
          ev.stopPropagation();
          const mid = btn.getAttribute('data-mid') || '';
          if (mid) cmpOpenHistory(mid);
          return;
        }
        const badge = ev.target.closest('.src-badge');
        if (badge && rows.contains(badge)) {
          ev.preventDefault();
          // P5 : si badge cliquable avec sources premium → popup détaillée
          if (badge.classList.contains('sb-sources-clickable')) {
            // Retrouver l'item via l'ID de la cellule parente
            const td = badge.closest('td');
            if (td && td.id && td.id.startsWith('sb-src-')) {
              const mid = td.id.slice(7); // retirer 'sb-src-'
              const item = Array.isArray(lastRenderedItems)
                ? lastRenderedItems.find(it => sneakerModelId(it.brand, it.model) === mid)
                : null;
              if (item) { showSourcesPopup(item); return; }
            }
          }
          // Fallback : modal simple count
          const count = Number(badge.getAttribute('data-count') || 0);
          const body = document.getElementById('src-modal-body');
          if (body) body.innerHTML = '<p style="font-size:0.95rem;margin:0">Ce produit est compar\u00e9 sur <strong>' + count + '</strong> boutique' + (count > 1 ? 's' : '') + ' fran\u00e7aise' + (count > 1 ? 's' : '') + '.</p>';
          const ov = document.getElementById('src-modal-overlay');
          if (ov) ov.classList.add('open');
        }
      });
    }
    const histOv = document.getElementById('modal-history-overlay');
    if (histOv) histOv.addEventListener('click', (e) => { if (e.target === histOv) cmpCloseHistory(); });

    function fetchWithTimeout(url, timeoutMs) {
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), timeoutMs);
      return fetch(url, { signal: ctrl.signal }).finally(() => clearTimeout(tid));
    }
    function simplifyModelName(name) {
      const txt = String(name || '').trim();
      if (!txt) return txt;
      const parts = txt.split(/\s+/).filter(Boolean);
      if (parts.length <= 2) return txt;
      return parts.slice(0, 2).join(' ');
    }
    showSkeletonRows();
    refreshHealthSignals();
    setInterval(refreshHealthSignals, 30000);

    // ── V2-first fetch strategy ───────────────────────────────────────────────
    // 1. Try /api/v2/comparison/fr  (strict matching, anti-aberration)
    // 2. If empty → fallback to /api/comparison/fr  (v1 aggregated)
    // 3. If v1 also empty → try v1 with relaxed filters
    function fetchV1Fallback(relaxed, simplifiedModel) {
      const qs2 = new URLSearchParams(qs.toString());
      if (simplifiedModel) qs2.set('model', simplifiedModel);
      if (relaxed) {
        qs2.set('validated_only', 'false');
        qs2.set('include_excluded', 'true');
      }
      return fetchWithTimeout('/api/comparison/fr?' + qs2.toString(), 8000)
        .then(r => { if (!r.ok && r.status === 401) throw new Error('auth'); return r.json(); })
        .then(data => {
          const arr = data.items || [];
          if (arr.length) { renderRows(arr, relaxed); updateMarketHealth(arr); return; }
          if (!relaxed) return fetchV1Fallback(true, simplifiedModel);
          const selectedModel = (modelSelect && modelSelect.value) || window.__CMP__.selectedModel || '';
          const simple = simplifyModelName(selectedModel);
          if (!simplifiedModel && simple && simple !== selectedModel) {
            console.warn('[CMP] Retry simplifié automatique', { brand: ((brandSelect && brandSelect.value) || window.__CMP__.selectedBrand || ''), model: selectedModel, simplified: simple });
            return fetchV1Fallback(false, simple);
          }
          if (refreshModeActive && Array.isArray(lastRenderedItems) && lastRenderedItems.length) {
            console.info('[UI] cached data preserved');
            return;
          }
          if (rows) rows.innerHTML = '<tr><td colspan="11" style="color:#9ca3af">Aucune donnée immédiate disponible. Réessayez ou changez variante.</td></tr>';
        });
    }

    fetchWithTimeout('/api/v2/comparison/fr?' + qs.toString(), 8000)
      .then(r => { if (!r.ok && r.status === 401) throw new Error('auth'); return r.json(); })
      .then(data => {
        const arr = data.items || [];
        if (arr.length) {
          renderRows(arr, false);   // v2 data — badges rendered by renderRows
          updateMarketHealth(arr);
          return;
        }
        // v2 has no data for this model yet — fall back to v1
        return fetchV1Fallback(false);
      })
      .catch(err => {
        if (err && err.message === 'auth') {
          if (rows) rows.innerHTML = '<tr><td colspan="11" style="color:#fca5a5">Session expirée. <a href="/login" style="color:#60a5fa">Reconnectez-vous</a>.</td></tr>';
          return;
        }
        if (err && err.name === 'AbortError') {
          // Timeout côté client — fallback local immédiat (v1/cache CSV)
          const brand = (brandSelect && brandSelect.value) || window.__CMP__.selectedBrand || '';
          const model = (modelSelect && modelSelect.value) || window.__CMP__.selectedModel || '';
          console.warn('[CMP] Timeout v2 -> fallback local immédiat', { brand, model });
          fetchV1Fallback(false).catch(function() {
            if (refreshModeActive && Array.isArray(lastRenderedItems) && lastRenderedItems.length) {
              console.info('[UI] cached data preserved');
              return;
            }
            if (rows) rows.innerHTML = '<tr><td colspan="11" style="color:#9ca3af">Aucune donnée immédiate disponible. Réessayez ou changez variante.</td></tr>';
          });
          return;
        }
        // v2 endpoint error → fall back silently to v1
        {
          const brand = (brandSelect && brandSelect.value) || window.__CMP__.selectedBrand || '';
          const model = (modelSelect && modelSelect.value) || window.__CMP__.selectedModel || '';
          console.warn('[CMP] Erreur v2 -> fallback local', { brand, model, err: String((err && err.message) || err || '') });
        }
        fetchV1Fallback(false).catch(() => {
          if (refreshModeActive && Array.isArray(lastRenderedItems) && lastRenderedItems.length) {
            console.info('[UI] cached data preserved');
            return;
          }
          if (rows) {
            rows.innerHTML = '<tr><td colspan="11" style="color:#9ca3af">Aucune donnée immédiate disponible. Réessayez ou changez variante.</td></tr>';
          }
        });
      });
    const srcOv = document.getElementById('src-modal-overlay');
    if (srcOv) srcOv.addEventListener('click', (e) => { if (e.target === srcOv) srcOv.classList.remove('open'); });
    const srcClose = document.getElementById('src-modal-close');
    if (srcClose) srcClose.addEventListener('click', () => { if (srcOv) srcOv.classList.remove('open'); });
})();
