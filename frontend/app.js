/* ==========================================================================
   PulseOps AI — Autonomous Financial & Operational Data Automation Workbench
   Modular SPA Client (Zero external dependencies, XSS-safe DOM rendering)
   ========================================================================== */

(function () {
  'use strict';

  const TAB_META = {
    overview: {
      title: 'Executive Overview',
      subtitle: 'Real-time budget variance, anomaly detection & SaaS spend intelligence',
    },
    data: {
      title: 'Data & SQL Explorer',
      subtitle: 'CSV ETL ingestion with deduplication & guarded read-only SQLite playground',
    },
    copilot: {
      title: 'AI Operations Copilot',
      subtitle: 'Deterministic SQL & Python tool-calling with auditable citations & execution traces',
    },
    workflows: {
      title: 'Automation Workflows',
      subtitle: 'One-click multi-step FinOps pipelines with executive briefs & Slack/Webhook payloads',
    },
    evals: {
      title: 'AI Evals & Reliability',
      subtitle: 'Automated assertions verifying numerical faithfulness, SQL guardrails & anomaly recall',
    },
  };

  const state = {
    activeTab: 'overview',
    theme: 'dark',
    health: null,
    overview: null,
    selectedDepartment: null,
    anomalySeverity: 'all',
    dataSummary: null,
    lastSqlResult: null,
    copilotMessages: [],
    selectedMessageIdx: -1,
    workflows: [],
    workflowHistory: [],
    activeWorkflowRun: null,
    activeWorkflowArtifactTab: 'brief',
    evals: null,
    evalCategoryFilter: null,
  };

  const MAX_CSV_BYTES = 4 * 1024 * 1024; // 4 MB client-side cap

  const inFlight = {
    reset: false,
    upload: false,
    sql: false,
    copilot: false,
    workflow: false,
    evals: false,
  };

  const reqSeq = {
    copilot: 0,
    sql: 0,
    workflow: 0,
    upload: 0,
    evals: 0,
    overview: 0,
    dataSummary: 0,
    health: 0,
  };

  const abortControllers = {
    copilot: null,
    sql: null,
  };

  /* ========================================================================
     Formatting & XSS-Safe Utilities
     ======================================================================== */

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatCurrency(val, showSign) {
    const num = Number(val) || 0;
    const absStr = '$' + Math.abs(num).toLocaleString('en-US', {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
    if (showSign) {
      if (num > 0) return '+' + absStr;
      if (num < 0) return '-' + absStr;
      return '$0';
    }
    return num < 0 ? '-' + absStr : absStr;
  }

  function formatCurrencyPrecise(val) {
    const num = Number(val) || 0;
    return (num < 0 ? '-' : '') + '$' + Math.abs(num).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function formatPercent(val, showSign) {
    const num = Number(val) || 0;
    const sign = showSign && num > 0 ? '+' : '';
    return sign + num.toFixed(1) + '%';
  }

  function normalizeEvalPct(score, passed, total) {
    if (passed !== null && passed !== undefined && total !== null && total !== undefined) {
      const t = Number(total);
      const p = Number(passed);
      if (Number.isFinite(t) && t > 0 && Number.isFinite(p)) {
        return Math.max(0, Math.min(100, (p / t) * 100));
      }
    }
    if (score === null || score === undefined || score === '' || isNaN(Number(score))) return null;
    const pct = Number(score);
    if (!Number.isFinite(pct)) return null;
    return Math.max(0, Math.min(100, pct));
  }

  function formatTimestamp(ts) {
    if (!ts) return '—';
    const raw = String(ts).trim();
    const iso = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(raw)
      ? raw.replace(' ', 'T') + (/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw) ? '' : 'Z')
      : raw;
    const d = new Date(iso);
    if (!isNaN(d.getTime())) {
      const pad2 = function (n) { return n < 10 ? '0' + n : String(n); };
      return (
        d.getUTCFullYear() + '-' +
        pad2(d.getUTCMonth() + 1) + '-' +
        pad2(d.getUTCDate()) + ' ' +
        pad2(d.getUTCHours()) + ':' +
        pad2(d.getUTCMinutes()) + ' UTC'
      );
    }
    return raw.length > 19 ? raw.slice(0, 19) : raw;
  }

  function formatMs(ms) {
    const num = Number(ms);
    if (isNaN(num)) return '—';
    return num < 10 ? num.toFixed(2) + ' ms' : num.toFixed(1) + ' ms';
  }

  function setButtonLoading(btn, isLoading, loadingText) {
    if (!btn) return;
    if (isLoading) {
      if (!btn.dataset.origHtml) btn.dataset.origHtml = btn.innerHTML;
      btn.disabled = true;
      btn.classList.add('is-loading');
      if (loadingText) btn.textContent = loadingText;
    } else {
      btn.disabled = false;
      btn.classList.remove('is-loading');
      if (btn.dataset.origHtml) {
        btn.innerHTML = btn.dataset.origHtml;
        delete btn.dataset.origHtml;
      }
    }
  }

  /**
   * Safe Markdown -> HTML converter.
   * Escapes all raw HTML first so user/CSV/SQL values can NEVER execute scripts.
   * Uses unique per-call sentinel tokens to avoid collisions with arbitrary text.
   */
  function renderSafeMarkdown(markdown) {
    const raw = String(markdown || '').replace(/\r\n/g, '\n');
    if (!raw.trim()) return '<p class="muted-text">No content generated.</p>';

    const nonce = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    const cbPrefix = 'PULSECB_' + nonce + '_';
    const icPrefix = 'PULSEIC_' + nonce + '_';
    const cbLineRe = new RegExp('^' + cbPrefix + '(\\d+)_$');
    const cbStartRe = new RegExp('^' + cbPrefix + '\\d+_$');
    const icTokenRe = new RegExp(icPrefix + '(\\d+)_', 'g');

    // Extract fenced code blocks first (surround with newlines so inline blocks never leak sentinels)
    const codeBlocks = [];
    const withPlaceholders = raw.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, function (_m, _lang, code) {
      const idx = codeBlocks.length;
      codeBlocks.push('<pre><code>' + escapeHtml(code.trimEnd()) + '</code></pre>');
      return '\n' + cbPrefix + idx + '_\n';
    });

    function inlineFormat(text) {
      let s = escapeHtml(text);
      const inlineCodes = [];
      s = s.replace(/`([^`]+)`/g, function (_m, code) {
        const idx = inlineCodes.length;
        inlineCodes.push('<code>' + code + '</code>');
        return icPrefix + idx + '_';
      });
      // Safe HTTPS-only markdown links
      s = s.replace(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g, function (full, label, href) {
        if (/(&quot;|&#39;|&lt;|&gt;|javascript:|data:)/i.test(href)) return full;
        return '<a class="md-link" href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
      });
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
      s = s.replace(/(^|[^\w])_(\S(?:.*?\S)?)_(?=[^\w]|$)/g, '$1<em>$2</em>');
      s = s.replace(icTokenRe, function (full, idx) {
        const n = Number(idx);
        return (n >= 0 && n < inlineCodes.length) ? inlineCodes[n] : full;
      });
      return s;
    }

    const lines = withPlaceholders.split('\n');
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) {
        i++;
        continue;
      }

      // Code block placeholder
      const cbMatch = trimmed.match(cbLineRe);
      if (cbMatch) {
        const idx = Number(cbMatch[1]);
        if (idx >= 0 && idx < codeBlocks.length) {
          out.push(codeBlocks[idx]);
        }
        i++;
        continue;
      }

      // Headings
      const hMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (hMatch) {
        const level = hMatch[1].length;
        out.push('<h' + level + '>' + inlineFormat(hMatch[2]) + '</h' + level + '>');
        i++;
        continue;
      }

      // Markdown table
      if (trimmed.indexOf('|') !== -1 && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
        const parseRow = function (r) {
          return r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) {
            return c.trim();
          });
        };
        const headers = parseRow(trimmed);
        i += 2;
        const bodyRows = [];
        while (i < lines.length && lines[i].trim().indexOf('|') !== -1) {
          bodyRows.push(parseRow(lines[i]));
          i++;
        }
        let tbl = '<table><thead><tr>' + headers.map(function (h) {
          return '<th>' + inlineFormat(h) + '</th>';
        }).join('') + '</tr></thead><tbody>';
        bodyRows.forEach(function (row) {
          tbl += '<tr>' + row.map(function (cell) {
            return '<td>' + inlineFormat(cell) + '</td>';
          }).join('') + '</tr>';
        });
        tbl += '</tbody></table>';
        out.push(tbl);
        continue;
      }

      // Unordered list
      if (/^[-*•]\s+/.test(trimmed)) {
        const items = [];
        while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) {
          items.push('<li>' + inlineFormat(lines[i].replace(/^\s*[-*•]\s+/, '')) + '</li>');
          i++;
        }
        out.push('<ul>' + items.join('') + '</ul>');
        continue;
      }

      // Ordered list
      if (/^\d+\.\s+/.test(trimmed)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push('<li>' + inlineFormat(lines[i].replace(/^\s*\d+\.\s+/, '')) + '</li>');
          i++;
        }
        out.push('<ol>' + items.join('') + '</ol>');
        continue;
      }

      // Horizontal rule
      if (/^---+$/.test(trimmed)) {
        out.push('<hr class="md-hr" />');
        i++;
        continue;
      }

      // Paragraph
      const paraLines = [trimmed];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^(#{1,4}\s+|[-*•]\s+|\d+\.\s+|---+$)/.test(lines[i].trim()) &&
        !cbStartRe.test(lines[i].trim()) &&
        !(lines[i].trim().indexOf('|') !== -1 && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1]))
      ) {
        paraLines.push(lines[i].trim());
        i++;
      }
      out.push('<p>' + inlineFormat(paraLines.join(' ')) + '</p>');
    }

    return out.join('\n');
  }

  /**
   * Builds a syntax-highlighted JSON DOM fragment using textContent nodes
   * so tokens match raw JSON accurately and HTML entities are never corrupted.
   */
  function renderHighlightedJson(obj) {
    const pretty = JSON.stringify(obj == null ? {} : obj, null, 2);
    const frag = document.createDocumentFragment();
    const tokenRe = /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g;
    let lastIndex = 0;
    let match;

    while ((match = tokenRe.exec(pretty)) !== null) {
      if (match.index > lastIndex) {
        frag.appendChild(document.createTextNode(pretty.slice(lastIndex, match.index)));
      }
      const token = match[0];
      let cls = 'json-number';
      if (token.charCodeAt(0) === 34) {
        cls = token.charCodeAt(token.length - 1) === 58 ? 'json-key' : 'json-string';
      } else if (token === 'true' || token === 'false') {
        cls = 'json-bool';
      } else if (token === 'null') {
        cls = 'json-null';
      }

      if (cls === 'json-key') {
        const colonIdx = token.lastIndexOf(':');
        const keySpan = document.createElement('span');
        keySpan.className = 'json-key';
        keySpan.textContent = token.slice(0, colonIdx);
        frag.appendChild(keySpan);
        frag.appendChild(document.createTextNode(token.slice(colonIdx)));
      } else {
        const span = document.createElement('span');
        span.className = cls;
        span.textContent = token;
        frag.appendChild(span);
      }
      lastIndex = tokenRe.lastIndex;
    }

    if (lastIndex < pretty.length) {
      frag.appendChild(document.createTextNode(pretty.slice(lastIndex)));
    }
    return frag;
  }

  /* ========================================================================
     Toast Notifications
     ======================================================================== */

  function showToast(title, message, type) {
    const stack = $('toast-stack');
    if (!stack) return;

    const toast = document.createElement('div');
    toast.className = 'toast' + (type ? ' toast-' + type : '');
    toast.setAttribute('role', 'status');

    const bodyWrap = document.createElement('div');
    const titleEl = document.createElement('div');
    titleEl.className = 'toast-title';
    titleEl.textContent = title;
    bodyWrap.appendChild(titleEl);

    if (message) {
      const msgEl = document.createElement('div');
      msgEl.className = 'toast-body';
      msgEl.textContent = message;
      bodyWrap.appendChild(msgEl);
    }

    const closeBtn = document.createElement('button');
    closeBtn.className = 'toast-close';
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Dismiss notification');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function () {
      toast.remove();
    });

    toast.appendChild(bodyWrap);
    toast.appendChild(closeBtn);
    stack.appendChild(toast);

    setTimeout(function () {
      if (toast.parentNode) toast.remove();
    }, 4500);
  }

  /* ========================================================================
     API Request Helper
     ======================================================================== */

  async function apiRequest(url, options) {
    const opts = options || {};
    const method = String(opts.method || 'GET').toUpperCase();
    const fetchOpts = {
      method: method,
      headers: Object.assign({ Accept: 'application/json' }, opts.headers || {}),
    };
    if (opts.signal) {
      fetchOpts.signal = opts.signal;
    }
    if (opts.body !== undefined) {
      fetchOpts.headers['Content-Type'] = 'application/json';
      fetchOpts.body = JSON.stringify(opts.body);
    } else if (method === 'POST' || method === 'PUT' || method === 'PATCH') {
      fetchOpts.headers['Content-Type'] = 'application/json';
      fetchOpts.body = '{}';
    }

    const res = await fetch(url, fetchOpts);
    const contentType = res.headers.get('content-type') || '';
    let payload;
    if (contentType.indexOf('application/json') !== -1) {
      payload = await res.json();
    } else {
      const text = await res.text();
      try {
        payload = JSON.parse(text);
      } catch (_e) {
        payload = { ok: res.ok, text: text };
      }
    }
    return { status: res.status, ok: res.ok, data: payload };
  }

  /* ========================================================================
     Theme & Navigation
     ======================================================================== */

  function initTheme() {
    let saved = 'dark';
    try {
      const stored = localStorage.getItem('pulseops_theme');
      if (stored === 'light' || stored === 'dark') saved = stored;
    } catch (_e) {
      saved = 'dark';
    }
    applyTheme(saved);

    const toggleBtn = $('btn-theme-toggle');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', function () {
        applyTheme(state.theme === 'dark' ? 'light' : 'dark');
      });
    }
  }

  function applyTheme(theme) {
    state.theme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', state.theme);
    try {
      localStorage.setItem('pulseops_theme', state.theme);
    } catch (_e) {
      // ignore storage errors in restricted environments
    }
  }

  function switchTab(tabId, skipHashUpdate, skipLoad) {
    const target = TAB_META[tabId] ? tabId : 'overview';
    state.activeTab = target;

    if (window.PulseCharts) window.PulseCharts.hideTooltip();

    document.querySelectorAll('.nav-item').forEach(function (btn) {
      const active = btn.dataset.tab === target;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.tabIndex = active ? 0 : -1;
    });

    document.querySelectorAll('.tab-panel').forEach(function (panel) {
      const active = panel.id === 'panel-' + target;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });

    const meta = TAB_META[target];
    if ($('topbar-title')) $('topbar-title').textContent = meta.title;
    if ($('topbar-subtitle')) $('topbar-subtitle').textContent = meta.subtitle;

    if (!skipHashUpdate && window.location.hash !== '#' + target) {
      try {
        window.history.replaceState(null, '', '#' + target);
      } catch (_e) {
        window.location.hash = '#' + target;
      }
    }

    if (skipLoad) return;

    // Ensure target tab data is populated
    if (target === 'overview' && !state.overview) loadOverview();
    if (target === 'data' && !state.dataSummary) loadDataSummary();
    if (target === 'workflows' && state.workflows.length === 0) loadWorkflows();
    if (target === 'evals' && !state.evals) loadLatestEvals();
  }

  function initNavigation() {
    const navButtons = Array.prototype.slice.call(document.querySelectorAll('.nav-item'));
    navButtons.forEach(function (btn, idx) {
      btn.addEventListener('click', function () {
        switchTab(btn.dataset.tab);
      });
      btn.addEventListener('keydown', function (ev) {
        let nextIdx = -1;
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') {
          nextIdx = (idx + 1) % navButtons.length;
        } else if (ev.key === 'ArrowUp' || ev.key === 'ArrowLeft') {
          nextIdx = (idx - 1 + navButtons.length) % navButtons.length;
        } else if (ev.key === 'Home') {
          nextIdx = 0;
        } else if (ev.key === 'End') {
          nextIdx = navButtons.length - 1;
        }
        if (nextIdx !== -1) {
          ev.preventDefault();
          switchTab(navButtons[nextIdx].dataset.tab);
          navButtons[nextIdx].focus();
        }
      });
    });

    window.addEventListener('hashchange', function () {
      const hashTab = (window.location.hash || '').replace(/^#/, '');
      if (TAB_META[hashTab] && hashTab !== state.activeTab) {
        switchTab(hashTab, true);
      }
    });

    const kpiEvalCard = $('kpi-eval-card');
    if (kpiEvalCard) {
      kpiEvalCard.addEventListener('click', function () {
        switchTab('evals');
      });
      kpiEvalCard.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          switchTab('evals');
        }
      });
    }
  }

  /* ========================================================================
     Health Status & Global Actions
     ======================================================================== */

  async function loadHealth() {
    const seq = ++reqSeq.health;
    const dot = $('db-status-dot');
    const text = $('db-status-text');
    const badge = $('db-status-badge');

    try {
      const res = await apiRequest('/api/health');
      if (seq !== reqSeq.health) return;
      if (!res.ok) throw new Error('Health endpoint returned ' + res.status);
      state.health = res.data;

      const tables = res.data.tables || {};
      const txCount = tables.transactions != null ? tables.transactions : Object.values(tables).reduce(function (a, b) {
        return a + (Number(b) || 0);
      }, 0);

      if (dot) dot.className = 'status-dot status-dot-live';
      if (text) text.textContent = 'SQLite Live · ' + txCount + ' txns';
      if (badge) {
        const tableList = Object.keys(tables).map(function (k) {
          return k + ': ' + tables[k];
        }).join(' | ');
        badge.title = 'DB: ' + (res.data.db_path || 'sqlite') + (tableList ? ' — ' + tableList : '');
      }

      const rowBadge = $('nav-badge-rows');
      if (rowBadge) {
        rowBadge.textContent = String(txCount);
        rowBadge.hidden = false;
      }

      updateCopilotProviderBadge();
    } catch (err) {
      if (seq !== reqSeq.health) return;
      if (dot) dot.className = 'status-dot status-dot-err';
      if (text) text.textContent = 'API Offline';
      if (badge) badge.title = String(err.message || err);
    }
  }

  function clearActiveSqlPreset() {
    const presetBar = $('sql-preset-chips');
    if (!presetBar) return;
    presetBar.querySelectorAll('.preset-chip.active').forEach(function (c) {
      c.classList.remove('active');
    });
  }

  function resetClientState() {
    // Cancel in-flight async requests and invalidate stale sequence tokens
    Object.keys(reqSeq).forEach(function (k) {
      reqSeq[k]++;
    });
    if (abortControllers.copilot) {
      abortControllers.copilot.abort();
      abortControllers.copilot = null;
    }
    if (abortControllers.sql) {
      abortControllers.sql.abort();
      abortControllers.sql = null;
    }
    inFlight.copilot = false;
    inFlight.sql = false;
    inFlight.workflow = false;
    inFlight.upload = false;
    inFlight.evals = false;

    setButtonLoading($('btn-copilot-send'), false);
    setButtonLoading($('btn-run-sql'), false);
    setButtonLoading($('btn-run-evals'), false);
    setButtonLoading($('btn-load-sample-csv'), false);
    setButtonLoading($('btn-run-monthly-close'), false);

    const dropzone = $('csv-dropzone');
    if (dropzone) {
      dropzone.classList.remove('is-uploading');
      dropzone.removeAttribute('aria-busy');
    }

    // 1. Overview state & severity filter chips
    state.selectedDepartment = null;
    state.anomalySeverity = 'all';
    if ($('btn-clear-dept-filter')) $('btn-clear-dept-filter').hidden = true;
    const filterGroup = $('anomaly-severity-filters');
    if (filterGroup) {
      filterGroup.querySelectorAll('.filter-chip').forEach(function (chip) {
        const isAll = (chip.dataset.severity || 'all') === 'all';
        chip.classList.toggle('active', isAll);
        chip.setAttribute('aria-pressed', isAll ? 'true' : 'false');
      });
    }

    // 2. Data & SQL Explorer state
    state.dataSummary = null;
    state.lastSqlResult = null;
    clearActiveSqlPreset();
    if ($('etl-result-banner')) $('etl-result-banner').hidden = true;
    if ($('sql-alert')) $('sql-alert').hidden = true;
    if ($('sql-results-meta')) $('sql-results-meta').hidden = true;
    if ($('sql-truncated-badge')) $('sql-truncated-badge').hidden = true;
    if ($('sql-latency-badge')) $('sql-latency-badge').hidden = true;
    if ($('sql-results-table')) $('sql-results-table').hidden = true;
    if ($('sql-results-thead')) $('sql-results-thead').textContent = '';
    if ($('sql-results-tbody')) $('sql-results-tbody').textContent = '';
    if ($('btn-export-sql-csv')) $('btn-export-sql-csv').disabled = true;
    if ($('sql-editor')) $('sql-editor').value = '';
    const sqlEmpty = $('sql-empty-state');
    if (sqlEmpty) {
      sqlEmpty.hidden = false;
      sqlEmpty.innerHTML = 'Select a preset query or enter a <code>SELECT</code> statement and click <strong>Run SQL</strong>.';
    }

    // 3. AI Copilot state
    state.copilotMessages = [];
    state.selectedMessageIdx = -1;
    renderCopilotMessages();
    renderToolTrace(null);

    // 4. Workflows state
    state.workflows = [];
    state.workflowHistory = [];
    state.activeWorkflowRun = null;
    state.activeWorkflowArtifactTab = 'brief';
    if ($('wf-output-empty')) $('wf-output-empty').hidden = false;
    if ($('wf-output-split')) $('wf-output-split').hidden = true;
    if ($('wf-output-actions')) $('wf-output-actions').hidden = true;
    if ($('wf-output-title')) $('wf-output-title').textContent = 'Live Pipeline Execution Output';
    if ($('wf-output-subtitle')) {
      $('wf-output-subtitle').textContent = 'Run any workflow above or click a historical run below to inspect steps, executive brief & Slack/Webhook JSON';
    }
    if ($('wf-step-timeline')) $('wf-step-timeline').textContent = '';
    if ($('wf-brief-view')) $('wf-brief-view').textContent = '';
    if ($('wf-json-view')) $('wf-json-view').textContent = '';
    setWorkflowArtifactTab('brief');

    // 5. AI Evals state
    state.evals = null;
    state.evalCategoryFilter = null;
    renderEvals(null);
  }

  async function handleResetDemoData() {
    if (inFlight.reset) return;
    inFlight.reset = true;
    const btn = $('btn-reset-demo');
    setButtonLoading(btn, true, 'Resetting...');
    try {
      const res = await apiRequest('/api/data/reset', { method: 'POST', body: {} });
      if (!res.ok || res.data.ok === false) {
        throw new Error(res.data.error || 'Reset failed');
      }
      resetClientState();
      showToast('Demo Data Reset', res.data.message || 'SQLite database reseeded cleanly.', 'success');

      // Load read-only eval state before overview so indicators never race
      await loadLatestEvals();
      await Promise.all([loadHealth(), loadOverview(), loadDataSummary(), loadWorkflows()]);
    } catch (err) {
      showToast('Reset Failed', err.message || 'Unable to reset database.', 'error');
    } finally {
      inFlight.reset = false;
      setButtonLoading(btn, false);
    }
  }

  /* ========================================================================
     Tab 1: Executive Overview
     ======================================================================== */

  async function loadOverview() {
    const seq = ++reqSeq.overview;
    try {
      const res = await apiRequest('/api/overview');
      if (seq !== reqSeq.overview) return;
      if (!res.ok) throw new Error(res.data.error || 'Failed to load overview');
      state.overview = res.data;
      renderOverview();
    } catch (err) {
      if (seq !== reqSeq.overview) return;
      showToast('Overview Error', err.message || 'Could not load overview metrics.', 'error');
    }
  }

  function renderOverview() {
    const data = state.overview;
    if (!data) return;

    const kpis = data.kpis || {};
    const totalBudget = Number(kpis.total_budget) || 0;
    const totalActual = Number(kpis.total_actual) || 0;
    const totalEac = Number(kpis.total_eac) || totalActual;
    const netDiff = totalActual - totalBudget;
    const varPct = totalBudget > 0 ? (netDiff / totalBudget) * 100 : (Number(kpis.variance_pct) || 0);
    const isOver = netDiff > 0;

    $('kpi-total-budget').textContent = formatCurrency(totalBudget);
    $('kpi-total-actual').textContent = formatCurrency(totalActual);
    $('kpi-eac-meta').textContent = 'Projected EAC: ' + formatCurrency(totalEac);

    const netVarEl = $('kpi-net-variance');
    netVarEl.textContent = formatCurrency(netDiff, true);
    netVarEl.className = 'kpi-value tabular ' + (isOver ? 'text-rose' : 'text-emerald');

    const varPill = $('kpi-variance-pill');
    varPill.textContent = (isOver ? 'Over ' : 'Under ') + formatPercent(varPct, true);
    varPill.className = 'pill tabular ' + (isOver ? 'pill-rose' : 'pill-emerald');

    const depts = data.department_variance || [];
    const overCount = depts.filter(function (d) { return d.status === 'over_budget'; }).length;
    $('kpi-budget-meta').textContent = depts.length + ' active departments tracked';
    $('kpi-variance-meta').textContent = overCount > 0
      ? (overCount + ' dept' + (overCount > 1 ? 's' : '') + ' over budget threshold')
      : 'All departments within budget guardrails';

    const anomalies = data.anomalies || [];
    const anomalyCount = kpis.anomaly_count != null ? Number(kpis.anomaly_count) : anomalies.length;
    const duplicateCount = kpis.duplicate_count != null ? Number(kpis.duplicate_count) : 0;
    const combinedAlerts = Math.max(anomalies.length, anomalyCount);
    const spikeCount = Math.max(0, combinedAlerts - duplicateCount);

    $('kpi-anomalies-total').textContent = String(combinedAlerts);
    $('kpi-anomalies-meta').textContent =
      spikeCount + ' spike' + (spikeCount === 1 ? '' : 's') + ' · ' +
      duplicateCount + ' duplicate' + (duplicateCount === 1 ? '' : 's');

    const navAnomBadge = $('nav-badge-anomalies');
    if (navAnomBadge) {
      navAnomBadge.textContent = String(combinedAlerts);
      navAnomBadge.hidden = combinedAlerts <= 0;
    }

    // Eval KPI Card
    const hasEvalState = state.evals && state.evals.has_run !== false && Number(state.evals.total) > 0;
    const evalScore = hasEvalState
      ? normalizeEvalPct(state.evals.pass_rate, state.evals.passed, state.evals.total)
      : normalizeEvalPct(kpis.last_eval_score);
    updateEvalIndicators(
      evalScore,
      hasEvalState ? state.evals.passed : null,
      hasEvalState ? state.evals.total : null
    );

    // Monthly SVG Chart + Burn Summary Strip
    const trend = data.monthly_trend || [];
    if (window.PulseCharts) {
      window.PulseCharts.renderMonthlyBudgetActualChart($('chart-monthly-trend'), trend);
    }
    renderMonthlySummaryStrip(trend, totalBudget, totalActual);

    // Department Chart + Breakdown Table
    renderDepartmentSection();

    // Anomalies Feed + Subscriptions Table
    renderAnomaliesFeed();
    renderSubscriptionsTable();
  }

  function renderMonthlySummaryStrip(trend, totalBudget, totalActual) {
    if (!Array.isArray(trend) || trend.length === 0) {
      ['ms-avg-spend', 'ms-peak-month', 'ms-over-months', 'ms-utilization'].forEach(function (id) {
        const el = $(id);
        if (el) {
          el.textContent = '--';
          el.className = 'monthly-stat-val tabular';
        }
      });
      return;
    }
    const avgSpend = totalActual / trend.length;
    let peak = trend[0];
    let overMonths = 0;
    trend.forEach(function (m) {
      const mActual = Number(m.actual) || 0;
      const mBudget = Number(m.budget) || 0;
      if (mActual > (Number(peak.actual) || 0)) peak = m;
      if (mBudget > 0 ? mActual > mBudget * 1.001 : mActual > 0) overMonths++;
    });
    const utilPct = totalBudget > 0 ? (totalActual / totalBudget) * 100 : null;

    if ($('ms-avg-spend')) $('ms-avg-spend').textContent = formatCurrency(avgSpend);
    if ($('ms-peak-month')) {
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const mMatch = String(peak.month || '').match(/^\d{4}-(\d{2})/);
      const mIdx = mMatch ? parseInt(mMatch[1], 10) - 1 : -1;
      const mShort = (mIdx >= 0 && mIdx < 12) ? months[mIdx] : String(peak.month || '—');
      const peakVal = Number(peak.actual) || 0;
      const peakShort = peakVal >= 1000 ? ('$' + Math.round(peakVal / 1000) + 'k') : formatCurrency(peakVal);
      $('ms-peak-month').textContent = mShort + ' (' + peakShort + ')';
    }
    if ($('ms-over-months')) {
      $('ms-over-months').textContent = overMonths + ' / ' + trend.length + ' mos';
      $('ms-over-months').className = 'monthly-stat-val tabular ' + (overMonths > 4 ? 'text-rose' : overMonths > 0 ? 'text-amber' : 'text-emerald');
    }
    if ($('ms-utilization')) {
      if (utilPct === null) {
        $('ms-utilization').textContent = totalActual > 0 ? 'N/A' : '0.0%';
        $('ms-utilization').className = 'monthly-stat-val tabular ' + (totalActual > 0 ? 'text-rose' : '');
      } else {
        $('ms-utilization').textContent = utilPct.toFixed(1) + '%';
        $('ms-utilization').className = 'monthly-stat-val tabular ' + (utilPct > 100 ? 'text-rose' : 'text-emerald');
      }
    }
  }

  function updateEvalIndicators(pct, passed, total) {
    const scoreEl = $('kpi-eval-score');
    const pillEl = $('kpi-eval-pill');
    const metaEl = $('kpi-eval-meta');
    const navBadge = $('nav-badge-evals');

    if (pct === null || pct === undefined) {
      if (scoreEl) {
        scoreEl.textContent = 'Run Suite';
        scoreEl.className = 'kpi-value tabular';
      }
      if (pillEl) {
        pillEl.textContent = 'Pending';
        pillEl.className = 'pill pill-neutral';
      }
      if (metaEl) metaEl.textContent = 'Click to run live reliability evals';
      if (navBadge) {
        navBadge.textContent = '--';
        navBadge.className = 'nav-badge nav-badge-neutral';
      }
      return;
    }

    const rounded = Math.round(pct) + '%';
    const isPass = pct >= 90;
    const isWarn = pct >= 70 && pct < 90;

    if (scoreEl) {
      scoreEl.textContent = rounded;
      scoreEl.className = 'kpi-value tabular ' + (isPass ? 'text-emerald' : isWarn ? 'text-amber' : 'text-rose');
    }
    if (pillEl) {
      pillEl.textContent = isPass ? 'Verified' : isWarn ? 'Watch' : 'Attention';
      pillEl.className = 'pill ' + (isPass ? 'pill-emerald' : isWarn ? 'pill-amber' : 'pill-rose');
    }
    if (metaEl) {
      metaEl.textContent = (passed != null && total != null)
        ? (passed + ' / ' + total + ' checks passing')
        : 'Latest automated eval pass rate';
    }
    if (navBadge) {
      navBadge.textContent = rounded;
      navBadge.className = 'nav-badge ' + (isPass ? 'nav-badge-emerald' : isWarn ? 'nav-badge-neutral' : 'nav-badge-rose');
    }
  }

  function toggleDepartmentFilter(dept) {
    state.selectedDepartment = state.selectedDepartment === dept ? null : dept;
    renderDepartmentSection();
    renderAnomaliesFeed();
    renderSubscriptionsTable();
  }

  function renderDepartmentSection() {
    const data = state.overview;
    if (!data) return;
    const depts = data.department_variance || [];

    const countBadge = $('dept-count-badge');
    if (countBadge) countBadge.textContent = depts.length + ' dept' + (depts.length === 1 ? '' : 's');

    const clearBtn = $('btn-clear-dept-filter');
    if (clearBtn) clearBtn.hidden = !state.selectedDepartment;

    if (window.PulseCharts) {
      window.PulseCharts.renderDepartmentVarianceChart($('chart-dept-variance'), depts, {
        selectedDepartment: state.selectedDepartment,
        onSelectDepartment: toggleDepartmentFilter,
      });
    }

    const tbody = $('dept-breakdown-body');
    if (!tbody) return;
    tbody.textContent = '';

    if (depts.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.className = 'empty-cell';
      td.textContent = 'No departments found.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    depts.forEach(function (d) {
      const isSelected = state.selectedDepartment === d.department;
      const tr = document.createElement('tr');
      tr.className = 'clickable-row' + (isSelected ? ' selected-row' : '');
      tr.tabIndex = 0;
      tr.setAttribute('role', 'button');
      tr.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
      tr.setAttribute('aria-label', 'Filter anomalies and subscriptions by ' + d.department + ' department');
      tr.title = 'Click to filter anomalies & subscriptions by ' + d.department;
      tr.addEventListener('click', function () {
        toggleDepartmentFilter(d.department);
      });
      tr.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          toggleDepartmentFilter(d.department);
        }
      });

      const budget = Number(d.budget) || 0;
      const actual = Number(d.actual) || 0;
      const diff = actual - budget;
      const hasBudget = budget > 0;
      const pct = hasBudget ? (diff / budget) * 100 : null;

      const statusMap = {
        on_track: { label: 'On Track', cls: 'pill-emerald' },
        watch: { label: 'Watch', cls: 'pill-amber' },
        over_budget: { label: 'Over Budget', cls: 'pill-rose' },
      };
      const st = statusMap[d.status] || { label: String(d.status || 'Unknown'), cls: 'pill-neutral' };

      const tdDept = document.createElement('td');
      tdDept.className = 'cell-strong';
      tdDept.textContent = d.department;

      const tdBudget = document.createElement('td');
      tdBudget.className = 'num';
      tdBudget.textContent = formatCurrency(budget);

      const tdActual = document.createElement('td');
      tdActual.className = 'num';
      tdActual.textContent = formatCurrency(actual);

      const tdVar = document.createElement('td');
      tdVar.className = 'num ' + (diff > 0 ? 'text-rose' : diff < 0 ? 'text-emerald' : '');
      const vAmt = document.createElement('span');
      vAmt.className = 'nowrap cell-strong';
      vAmt.textContent = formatCurrency(diff, true);
      const vPct = document.createElement('span');
      vPct.className = 'cell-sub-inherit nowrap';
      vPct.textContent = hasBudget ? formatPercent(pct, true) : (actual > 0 ? 'Unbudgeted' : 'N/A');
      tdVar.append(vAmt, vPct);

      const tdEac = document.createElement('td');
      tdEac.className = 'num';
      tdEac.textContent = formatCurrency(d.eac);

      const tdStatus = document.createElement('td');
      const pill = document.createElement('span');
      pill.className = 'pill ' + st.cls;
      pill.textContent = st.label;
      tdStatus.appendChild(pill);

      tr.append(tdDept, tdBudget, tdActual, tdVar, tdEac, tdStatus);
      tbody.appendChild(tr);
    });
  }

  function renderAnomaliesFeed() {
    const feed = $('anomalies-feed');
    if (!feed || !state.overview) return;
    feed.textContent = '';

    const all = state.overview.anomalies || [];
    const filtered = all.filter(function (item) {
      if (state.selectedDepartment && item.department !== state.selectedDepartment) return false;
      if (state.anomalySeverity !== 'all' && String(item.severity || '').toLowerCase() !== state.anomalySeverity) return false;
      return true;
    });

    const sub = $('anomalies-subtitle');
    if (sub) {
      sub.textContent = state.selectedDepartment
        ? ('Filtered to ' + state.selectedDepartment + ' department (' + filtered.length + ' items)')
        : 'Deterministic z-score spend spikes & duplicate invoice audit';
    }

    if (filtered.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = 'No anomalies match the current filter.';
      feed.appendChild(empty);
      return;
    }

    filtered.forEach(function (a) {
      const sev = String(a.severity || 'medium').toLowerCase();
      const card = document.createElement('div');
      card.className = 'anomaly-item severity-' + (sev === 'high' ? 'high' : 'medium');

      const top = document.createElement('div');
      top.className = 'anomaly-top';

      const left = document.createElement('div');
      left.className = 'anomaly-left';

      const sevPill = document.createElement('span');
      sevPill.className = 'pill ' + (sev === 'high' ? 'pill-rose' : 'pill-amber');
      sevPill.textContent = sev.toUpperCase();

      const typeLabel = String(a.type || 'anomaly').replace(/_/g, ' ');
      const typePill = document.createElement('span');
      typePill.className = 'pill pill-neutral';
      typePill.textContent = typeLabel;

      const vendor = document.createElement('span');
      vendor.className = 'anomaly-vendor';
      vendor.textContent = a.vendor || 'Unknown Vendor';

      left.append(sevPill, typePill, vendor);

      const amount = document.createElement('span');
      amount.className = 'anomaly-amount tabular';
      amount.textContent = formatCurrencyPrecise(a.amount);

      top.append(left, amount);

      const detail = document.createElement('div');
      detail.className = 'anomaly-detail';
      detail.textContent = a.detail || '';

      const meta = document.createElement('div');
      meta.className = 'anomaly-meta';

      const deptSpan = document.createElement('span');
      deptSpan.textContent = 'Dept: ' + (a.department || '—');

      const invSpan = document.createElement('span');
      invSpan.className = 'mono';
      invSpan.textContent = 'Invoice: ' + (a.invoice_id || '—');

      const dateSpan = document.createElement('span');
      dateSpan.className = 'tabular';
      dateSpan.textContent = a.tx_date || '—';

      meta.append(deptSpan, invSpan, dateSpan);
      card.append(top, detail, meta);
      feed.appendChild(card);
    });
  }

  function renderSubscriptionsTable() {
    const tbody = $('subscriptions-body');
    if (!tbody || !state.overview) return;
    tbody.textContent = '';

    const kpis = state.overview.kpis || {};
    const subs = (state.overview.subscriptions || []).filter(function (s) {
      return !state.selectedDepartment || s.department === state.selectedDepartment;
    });

    const savingsEl = $('sub-savings-opp');
    if (savingsEl) {
      if (!state.selectedDepartment) {
        savingsEl.textContent = formatCurrency(kpis.subscription_savings_opp || 0);
      } else {
        let monthlySavings = 0;
        subs.forEach(function (s) {
          const monthly = Number(s.monthly_cost) || 0;
          const prev = Number(s.prev_monthly_cost) || 0;
          const pct = Number(s.price_change_pct) || 0;
          const days = Number(s.last_used_days) || 0;
          if (days > 90) {
            monthlySavings += monthly;
          } else if (pct >= 15) {
            monthlySavings += Math.max(0, monthly - prev);
          }
        });
        savingsEl.textContent = formatCurrency(Math.round(monthlySavings * 12 * 100) / 100);
      }
    }

    if (subs.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 5;
      td.className = 'empty-cell';
      td.textContent = 'No SaaS subscriptions in this filter.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    subs.forEach(function (s) {
      const tr = document.createElement('tr');

      const tdVendor = document.createElement('td');
      const vName = document.createElement('span');
      vName.className = 'cell-strong';
      vName.textContent = s.vendor || '—';
      const vDept = document.createElement('span');
      vDept.className = 'cell-sub';
      vDept.textContent = s.department || '';
      tdVendor.append(vName, vDept);

      const tdCost = document.createElement('td');
      tdCost.className = 'num';
      tdCost.textContent = formatCurrency(s.monthly_cost) + '/mo';

      const pct = Number(s.price_change_pct) || 0;
      const tdDelta = document.createElement('td');
      tdDelta.className = 'num ' + (pct >= 15 ? 'text-rose' : pct > 0 ? 'text-amber' : 'muted-text');
      tdDelta.textContent = pct === 0 ? '0.0%' : formatPercent(pct, true);

      const days = Number(s.last_used_days) || 0;
      const tdDays = document.createElement('td');
      tdDays.className = 'num ' + (days > 90 ? 'text-rose' : days > 45 ? 'text-amber' : '');
      tdDays.textContent = days + 'd';

      const tdFlag = document.createElement('td');
      const pill = document.createElement('span');
      const flagText = s.flag || (s.status && s.status !== 'active' && s.status !== 'healthy' ? s.status : null);
      if (flagText) {
        const lower = String(flagText).toLowerCase();
        const isHigh = lower.indexOf('inactive') !== -1 || lower.indexOf('unused') !== -1 || lower.indexOf('hike') !== -1 || days > 90;
        pill.className = 'pill ' + (isHigh ? 'pill-rose' : 'pill-amber');
        pill.textContent = String(flagText).replace(/_/g, ' ');
      } else {
        pill.className = 'pill pill-emerald';
        pill.textContent = 'Healthy';
      }
      tdFlag.appendChild(pill);

      tr.append(tdVendor, tdCost, tdDelta, tdDays, tdFlag);
      tbody.appendChild(tr);
    });
  }

  function initOverviewControls() {
    const clearBtn = $('btn-clear-dept-filter');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        state.selectedDepartment = null;
        renderDepartmentSection();
        renderAnomaliesFeed();
        renderSubscriptionsTable();
      });
    }

    const filterGroup = $('anomaly-severity-filters');
    if (filterGroup) {
      filterGroup.addEventListener('click', function (ev) {
        const btn = ev.target.closest('.filter-chip');
        if (!btn) return;
        state.anomalySeverity = btn.dataset.severity || 'all';
        filterGroup.querySelectorAll('.filter-chip').forEach(function (c) {
          const active = c === btn;
          c.classList.toggle('active', active);
          c.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        renderAnomaliesFeed();
      });
    }
  }

  /* ========================================================================
     Tab 2: Data & SQL Explorer
     ======================================================================== */

  async function loadDataSummary() {
    const seq = ++reqSeq.dataSummary;
    try {
      const res = await apiRequest('/api/data/summary');
      if (seq !== reqSeq.dataSummary) return;
      if (!res.ok) throw new Error(res.data.error || 'Failed to load data summary');
      state.dataSummary = res.data;
      renderDataSummary();
    } catch (err) {
      if (seq !== reqSeq.dataSummary) return;
      showToast('Data Explorer Error', err.message || 'Could not load database summary.', 'error');
    }
  }

  function renderDataSummary() {
    const summary = state.dataSummary;
    if (!summary) return;

    // 1. Schema Table Chips
    const schemaWrap = $('schema-table-chips');
    if (schemaWrap) {
      schemaWrap.textContent = '';
      const tables = summary.tables || [];
      if (tables.length === 0) {
        schemaWrap.textContent = 'No tables found.';
      } else {
        tables.forEach(function (t) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'schema-chip';
          btn.title = 'Columns: ' + (Array.isArray(t.columns) ? t.columns.join(', ') : '');

          const nameSpan = document.createElement('span');
          nameSpan.textContent = t.name;

          const rowCount = t.row_count != null ? Number(t.row_count) : 0;
          const countSpan = document.createElement('span');
          countSpan.className = 'schema-chip-count';
          countSpan.textContent = rowCount + ' row' + (rowCount === 1 ? '' : 's');

          btn.append(nameSpan, countSpan);
          btn.addEventListener('click', function () {
            const safeTable = String(t.name || '').replace(/[^a-zA-Z0-9_]/g, '');
            if (!safeTable) return;
            clearActiveSqlPreset();
            $('sql-editor').value = 'SELECT * FROM ' + safeTable + ' LIMIT 20;';
            executeSqlQuery(false);
          });
          schemaWrap.appendChild(btn);
        });
      }
    }

    // 2. ETL Run History
    const runs = summary.etl_runs || [];
    if ($('etl-runs-count')) $('etl-runs-count').textContent = runs.length + ' run' + (runs.length === 1 ? '' : 's');
    const tbody = $('etl-runs-body');
    if (tbody) {
      tbody.textContent = '';
      if (runs.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.className = 'empty-cell';
        td.textContent = 'No CSV ETL runs yet. Upload a CSV or click Load & Ingest Sample Q4 CSV.';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        runs.forEach(function (r) {
          const tr = document.createElement('tr');

          const tdFile = document.createElement('td');
          const fileNameText = r.filename || r.source || 'upload.csv';
          tdFile.title = fileNameText;
          const fName = document.createElement('span');
          fName.className = 'cell-strong truncate-cell';
          fName.textContent = fileNameText;
          const fTime = document.createElement('span');
          fTime.className = 'cell-sub tabular';
          fTime.textContent = formatTimestamp(r.ran_at || r.created_at || r.timestamp);
          tdFile.append(fName, fTime);

          const inserted = r.rows_inserted != null ? r.rows_inserted : (r.inserted_count != null ? r.inserted_count : 0);
          const dupes = r.duplicates_skipped != null ? r.duplicates_skipped : (r.duplicate_count != null ? r.duplicate_count : 0);
          const anoms = r.anomalies_flagged != null ? r.anomalies_flagged : (r.anomalies_count != null ? r.anomalies_count : 0);

          const tdIns = document.createElement('td');
          tdIns.className = 'num text-emerald';
          tdIns.textContent = String(inserted);

          const tdDup = document.createElement('td');
          tdDup.className = 'num ' + (dupes > 0 ? 'text-amber' : '');
          tdDup.textContent = String(dupes);

          const tdAnom = document.createElement('td');
          tdAnom.className = 'num ' + (anoms > 0 ? 'text-rose' : '');
          tdAnom.textContent = String(anoms);

          const tdSt = document.createElement('td');
          const pill = document.createElement('span');
          const stText = String(r.status || 'completed');
          pill.className = 'pill ' + (stText === 'failed' ? 'pill-rose' : 'pill-emerald');
          pill.textContent = stText;
          tdSt.appendChild(pill);

          tr.append(tdFile, tdIns, tdDup, tdAnom, tdSt);
          tbody.appendChild(tr);
        });
      }
    }

    // 3. Preset SQL Query Chips
    const presetBar = $('sql-preset-chips');
    const presets = summary.preset_queries || [];
    if (presetBar) {
      const prevActiveChip = presetBar.querySelector('.preset-chip.active');
      const prevActiveLabel = prevActiveChip ? prevActiveChip.textContent : null;
      presetBar.textContent = '';
      presets.forEach(function (pq, idx) {
        const chip = document.createElement('button');
        chip.type = 'button';
        const label = pq.label || pq.id || ('Query ' + (idx + 1));
        chip.className = 'preset-chip' + (prevActiveLabel && prevActiveLabel === label ? ' active' : '');
        chip.textContent = label;
        chip.addEventListener('click', function () {
          presetBar.querySelectorAll('.preset-chip').forEach(function (c) {
            c.classList.toggle('active', c === chip);
          });
          $('sql-editor').value = pq.sql || '';
          executeSqlQuery(false);
        });
        presetBar.appendChild(chip);
      });

      // Seed default SQL editor with first preset if empty
      const editor = $('sql-editor');
      if (editor && !editor.value.trim() && presets.length > 0) {
        editor.value = presets[0].sql || '';
      }
    }
  }

  function setUploadBusy(busy) {
    inFlight.upload = busy;
    const dropzone = $('csv-dropzone');
    if (dropzone) {
      dropzone.classList.toggle('is-uploading', busy);
      if (busy) dropzone.setAttribute('aria-busy', 'true');
      else dropzone.removeAttribute('aria-busy');
    }
    const sampleBtn = $('btn-load-sample-csv');
    if (sampleBtn && !sampleBtn.classList.contains('is-loading')) {
      sampleBtn.disabled = busy;
    }
  }

  async function ingestCsvText(filename, csvText, seqToken) {
    const seq = seqToken != null ? seqToken : ++reqSeq.upload;
    const manageLock = seqToken == null;
    if (manageLock) {
      if (inFlight.upload) return;
      setUploadBusy(true);
    }

    const banner = $('etl-result-banner');
    try {
      const res = await apiRequest('/api/data/upload', {
        method: 'POST',
        body: { filename: filename, csv_text: csvText },
      });
      if (seq !== reqSeq.upload) return;

      if (!res.ok || res.data.ok === false) {
        const errMsg = res.data.error || ('Upload rejected (HTTP ' + res.status + ')');
        if (banner) {
          banner.hidden = false;
          banner.className = 'etl-banner is-error';
          banner.textContent = 'ETL Validation Error: ' + errMsg;
        }
        showToast('CSV Rejected', errMsg, 'error');
        return;
      }

      const inserted = res.data.inserted_count != null ? res.data.inserted_count : 0;
      const skipped = res.data.duplicates_skipped != null ? res.data.duplicates_skipped : 0;
      const detected = Array.isArray(res.data.anomalies_detected) ? res.data.anomalies_detected : [];

      if (banner) {
        banner.hidden = false;
        banner.className = 'etl-banner';
        banner.textContent = '';

        const title = document.createElement('div');
        title.className = 'etl-banner-title';
        const leftTxt = document.createElement('span');
        leftTxt.textContent = 'Ingested ' + filename;
        const okPill = document.createElement('span');
        okPill.className = 'pill pill-emerald';
        okPill.textContent = 'ETL Completed';
        title.append(leftTxt, okPill);

        const metrics = document.createElement('div');
        metrics.className = 'etl-metrics';

        const p1 = document.createElement('span');
        p1.className = 'pill pill-emerald tabular';
        p1.textContent = '+' + inserted + ' row' + (inserted === 1 ? '' : 's') + ' inserted';

        const p2 = document.createElement('span');
        p2.className = 'pill pill-amber tabular';
        p2.textContent = skipped + ' duplicate' + (skipped === 1 ? '' : 's') + ' skipped';

        const p3 = document.createElement('span');
        p3.className = 'pill ' + (detected.length > 0 ? 'pill-rose' : 'pill-neutral') + ' tabular';
        p3.textContent = detected.length + ' new anomal' + (detected.length === 1 ? 'y' : 'ies') + ' flagged';

        metrics.append(p1, p2, p3);
        banner.append(title, metrics);
      }

      showToast(
        'CSV Ingested',
        inserted + ' inserted · ' + skipped + ' duplicate' + (skipped === 1 ? '' : 's') + ' skipped · ' + detected.length + ' flagged.',
        'success'
      );

      await Promise.all([loadHealth(), loadOverview(), loadDataSummary()]);
    } catch (err) {
      if (seq !== reqSeq.upload) return;
      showToast('Upload Failed', err.message || 'Network error while uploading CSV.', 'error');
    } finally {
      if (manageLock && seq === reqSeq.upload) {
        setUploadBusy(false);
      }
    }
  }

  async function handleLoadSampleCsv() {
    if (inFlight.upload) return;
    const seq = ++reqSeq.upload;
    const btn = $('btn-load-sample-csv');
    setButtonLoading(btn, true, 'Ingesting Sample...');
    setUploadBusy(true);
    try {
      const resp = await fetch('/api/sample-csv');
      if (seq !== reqSeq.upload) return;
      if (!resp.ok) throw new Error('Could not fetch /api/sample-csv (HTTP ' + resp.status + ')');
      const csvText = await resp.text();
      if (seq !== reqSeq.upload) return;
      await ingestCsvText('q4_upload_sample.csv', csvText, seq);
    } catch (err) {
      if (seq !== reqSeq.upload) return;
      showToast('Sample CSV Error', err.message || 'Failed to load sample CSV.', 'error');
    } finally {
      if (seq === reqSeq.upload) {
        setButtonLoading(btn, false);
        setUploadBusy(false);
      }
    }
  }

  function readAndUploadFile(file) {
    if (!file || inFlight.upload) return;

    const fileName = String(file.name || '');
    const fileType = String(file.type || '').toLowerCase();
    const isCsvExt = /\.csv$/i.test(fileName);
    const isCsvMime = fileType === 'text/csv';

    if (!isCsvExt && !isCsvMime) {
      showToast('Invalid File Type', 'Only .csv files are accepted (received "' + (fileName || 'unknown') + '").', 'error');
      return;
    }
    if (file.size > MAX_CSV_BYTES) {
      const mb = (file.size / (1024 * 1024)).toFixed(1);
      showToast('File Too Large', 'CSV files must be 4 MB or smaller (' + mb + ' MB selected).', 'error');
      return;
    }
    if (file.size === 0) {
      showToast('Empty File', 'The selected CSV file is empty.', 'warning');
      return;
    }

    const seq = ++reqSeq.upload;
    setUploadBusy(true);
    const reader = new FileReader();
    reader.onload = async function () {
      if (seq !== reqSeq.upload) return;
      try {
        await ingestCsvText(fileName || 'uploaded.csv', String(reader.result || ''), seq);
      } finally {
        if (seq === reqSeq.upload) setUploadBusy(false);
      }
    };
    reader.onerror = function () {
      if (seq === reqSeq.upload) setUploadBusy(false);
      showToast('File Error', 'Could not read selected CSV file.', 'error');
    };
    reader.readAsText(file);
  }

  async function executeSqlQuery(isGuardTest) {
    const editor = $('sql-editor');
    const sql = editor ? editor.value.trim() : '';
    if (!sql) {
      showToast('Empty Query', 'Enter a read-only SELECT statement to run.', 'warning');
      return;
    }

    // Cancel any slower prior SQL request so newer queries always win cleanly
    if (abortControllers.sql) {
      abortControllers.sql.abort();
    }
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    abortControllers.sql = ctrl;
    const seq = ++reqSeq.sql;
    inFlight.sql = true;

    const runBtn = $('btn-run-sql');
    const alertBox = $('sql-alert');
    const metaBar = $('sql-results-meta');
    const truncBadge = $('sql-truncated-badge');
    const tableEl = $('sql-results-table');
    const emptyEl = $('sql-empty-state');
    const latencyBadge = $('sql-latency-badge');
    const exportBtn = $('btn-export-sql-csv');

    setButtonLoading(runBtn, true, 'Running...');
    if (alertBox) alertBox.hidden = true;

    try {
      const res = await apiRequest('/api/sql/query', {
        method: 'POST',
        body: { sql: sql },
        signal: ctrl ? ctrl.signal : undefined,
      });
      if (seq !== reqSeq.sql) return;

      if (!res.ok || res.data.ok === false) {
        const errMsg = res.data.error || ('Query blocked (HTTP ' + res.status + ')');
        state.lastSqlResult = null;
        if (exportBtn) exportBtn.disabled = true;
        if (latencyBadge) latencyBadge.hidden = true;
        if (metaBar) metaBar.hidden = true;
        if (truncBadge) truncBadge.hidden = true;
        if (tableEl) tableEl.hidden = true;
        if (emptyEl) emptyEl.hidden = true;
        if ($('sql-results-thead')) $('sql-results-thead').textContent = '';
        if ($('sql-results-tbody')) $('sql-results-tbody').textContent = '';

        if (alertBox) {
          alertBox.hidden = false;
          alertBox.textContent = '';

          const icon = document.createElement('div');
          icon.className = 'sql-alert-icon';
          icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M10 2.5L16.5 5V9.8C16.5 13.9 13.7 16.9 10 18.2C6.3 16.9 3.5 13.9 3.5 9.8V5L10 2.5Z"/><path d="M7.5 7.5L12.5 12.5M12.5 7.5L7.5 12.5"/></svg>';

          const content = document.createElement('div');
          const title = document.createElement('div');
          title.className = 'sql-alert-title';
          title.textContent = isGuardTest
            ? 'Read-Only SQL Safety Guard Triggered (Blocked as Expected)'
            : 'SQL Guardrail / Execution Error';
          const msg = document.createElement('div');
          msg.className = 'sql-alert-msg';
          msg.textContent = errMsg;

          content.append(title, msg);
          alertBox.append(icon, content);
        }

        if (isGuardTest) {
          showToast('SQL Guard Verified', 'Destructive query was strictly blocked by server.', 'success');
        } else {
          showToast('Query Blocked', errMsg, 'warning');
        }
        return;
      }

      const columns = res.data.columns || [];
      const rows = res.data.rows || [];
      const rowCount = res.data.row_count != null ? res.data.row_count : rows.length;
      const durationMs = res.data.duration_ms;
      const isTruncated = Boolean(res.data.truncated);

      state.lastSqlResult = { columns: columns, rows: rows };
      if (exportBtn) exportBtn.disabled = columns.length === 0;

      if (latencyBadge) {
        latencyBadge.textContent = formatMs(durationMs);
        latencyBadge.hidden = false;
      }
      if (metaBar) metaBar.hidden = false;
      if ($('sql-row-count')) $('sql-row-count').textContent = rowCount + ' row' + (rowCount === 1 ? '' : 's') + ' returned';
      if ($('sql-col-count')) $('sql-col-count').textContent = columns.length + ' column' + (columns.length === 1 ? '' : 's');
      if (truncBadge) {
        truncBadge.hidden = !isTruncated;
        if (isTruncated) {
          truncBadge.textContent = 'Showing first ' + rowCount + ' rows (truncated)';
        }
      }

      const thead = $('sql-results-thead');
      const tbody = $('sql-results-tbody');
      thead.textContent = '';
      tbody.textContent = '';

      if (columns.length === 0) {
        tableEl.hidden = true;
        emptyEl.hidden = false;
        emptyEl.textContent = 'Query succeeded with 0 columns.';
        return;
      }

      const normalizedRows = rows.map(function (row) {
        return Array.isArray(row) ? row : columns.map(function (c) { return row[c]; });
      });

      // Right-align <th> headers for columns whose non-null values are numeric
      const numericCols = columns.map(function (_col, colIdx) {
        let sawValue = false;
        for (let r = 0; r < normalizedRows.length; r++) {
          const val = normalizedRows[r][colIdx];
          if (val === null || val === undefined) continue;
          sawValue = true;
          if (typeof val !== 'number') return false;
        }
        return sawValue;
      });

      const headTr = document.createElement('tr');
      columns.forEach(function (col, colIdx) {
        const th = document.createElement('th');
        if (numericCols[colIdx]) th.className = 'num';
        th.textContent = String(col);
        headTr.appendChild(th);
      });
      thead.appendChild(headTr);

      if (normalizedRows.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = columns.length;
        td.className = 'empty-cell';
        td.textContent = '0 rows matched this query.';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        normalizedRows.forEach(function (cells) {
          const tr = document.createElement('tr');
          cells.forEach(function (val, colIdx) {
            const td = document.createElement('td');
            if (typeof val === 'number') {
              td.className = 'num';
              td.textContent = Number.isInteger(val) ? val.toLocaleString('en-US') : val.toLocaleString('en-US', { maximumFractionDigits: 4 });
            } else if (val === null || val === undefined) {
              td.className = numericCols[colIdx] ? 'num muted-text' : 'muted-text';
              td.textContent = 'NULL';
            } else {
              td.textContent = String(val);
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      }

      emptyEl.hidden = true;
      tableEl.hidden = false;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (seq !== reqSeq.sql) return;
      showToast('SQL Request Failed', err.message || 'Could not reach /api/sql/query.', 'error');
    } finally {
      if (seq === reqSeq.sql) {
        inFlight.sql = false;
        abortControllers.sql = null;
        setButtonLoading(runBtn, false);
      }
    }
  }

  function exportSqlResultsCsv() {
    const result = state.lastSqlResult;
    if (!result || !result.columns || result.columns.length === 0) return;

    function csvEscape(v) {
      if (v === null || v === undefined) return '';
      let s = String(v);
      // CWE-1236: Neutralize spreadsheet formula injection on non-numeric cells
      if (typeof v !== 'number' && (/^[\t\r\n ]*[=+\-@]/.test(s) || /^[\t\r]/.test(s))) {
        s = "'" + s;
      }
      if (/[",\n\r]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }

    const lines = [result.columns.map(csvEscape).join(',')];
    result.rows.forEach(function (row) {
      const cells = Array.isArray(row) ? row : result.columns.map(function (c) { return row[c]; });
      lines.push(cells.map(csvEscape).join(','));
    });

    // Prefix UTF-8 BOM so Windows Excel opens UTF-8 characters cleanly
    downloadTextFile('pulseops_sql_export.csv', '\ufeff' + lines.join('\r\n'), 'text/csv;charset=utf-8');
    showToast('CSV Exported', result.rows.length + ' row' + (result.rows.length === 1 ? '' : 's') + ' downloaded.', 'success');
  }

  function downloadTextFile(filename, content, mimeType) {
    const blob = new Blob([content], { type: mimeType || 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function initDataTabControls() {
    const dropzone = $('csv-dropzone');
    const fileInput = $('csv-file-input');

    if (dropzone && fileInput) {
      dropzone.addEventListener('click', function (ev) {
        if (ev.target === fileInput || inFlight.upload) return;
        fileInput.click();
      });
      dropzone.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          if (!inFlight.upload) fileInput.click();
        }
      });

      fileInput.addEventListener('change', function () {
        if (fileInput.files && fileInput.files[0]) {
          readAndUploadFile(fileInput.files[0]);
          fileInput.value = '';
        }
      });

      ['dragenter', 'dragover'].forEach(function (evt) {
        dropzone.addEventListener(evt, function (ev) {
          ev.preventDefault();
          if (!inFlight.upload) dropzone.classList.add('is-dragover');
        });
      });
      ['dragleave', 'drop'].forEach(function (evt) {
        dropzone.addEventListener(evt, function (ev) {
          ev.preventDefault();
          dropzone.classList.remove('is-dragover');
        });
      });
      dropzone.addEventListener('drop', function (ev) {
        if (inFlight.upload) return;
        const files = ev.dataTransfer && ev.dataTransfer.files;
        if (files && files[0]) readAndUploadFile(files[0]);
      });
    }

    if ($('btn-load-sample-csv')) {
      $('btn-load-sample-csv').addEventListener('click', handleLoadSampleCsv);
    }

    if ($('btn-run-sql')) {
      $('btn-run-sql').addEventListener('click', function () { executeSqlQuery(false); });
    }

    if ($('btn-test-sql-guard')) {
      $('btn-test-sql-guard').addEventListener('click', function () {
        clearActiveSqlPreset();
        $('sql-editor').value = 'DROP TABLE transactions;';
        executeSqlQuery(true);
      });
    }

    if ($('btn-export-sql-csv')) {
      $('btn-export-sql-csv').addEventListener('click', exportSqlResultsCsv);
    }

    if ($('sql-editor')) {
      $('sql-editor').addEventListener('input', clearActiveSqlPreset);
      $('sql-editor').addEventListener('keydown', function (ev) {
        if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') {
          ev.preventDefault();
          executeSqlQuery(false);
        }
      });
    }
  }

  /* ========================================================================
     Tab 3: AI Copilot
     ======================================================================== */

  function updateCopilotProviderBadge() {
    const select = $('copilot-provider');
    const pill = $('copilot-provider-status');
    const keyInput = $('copilot-api-key');
    if (!select || !pill) return;

    const provider = select.value;
    const providers = (state.health && state.health.llm_providers) || {};
    const hasKeyInInput = keyInput && keyInput.value.trim().length > 0;

    if (provider === 'local') {
      pill.textContent = 'Offline Ready';
      pill.className = 'pill pill-emerald';
    } else if (hasKeyInInput) {
      pill.textContent = 'Session Key Ready';
      pill.className = 'pill pill-cyan';
    } else if (providers[provider]) {
      pill.textContent = 'Env Key Configured';
      pill.className = 'pill pill-emerald';
    } else {
      pill.textContent = 'Optional Key (Auto-Fallback)';
      pill.className = 'pill pill-amber';
    }
  }

  async function askCopilot(questionText) {
    if (inFlight.copilot) return;
    const question = String(questionText || '').trim();
    if (!question) return;

    const provider = $('copilot-provider') ? $('copilot-provider').value : 'local';
    const rawKey = $('copilot-api-key') ? $('copilot-api-key').value.trim() : '';
    const apiKey = provider === 'local' ? '' : rawKey;
    const sendBtn = $('btn-copilot-send');

    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    abortControllers.copilot = ctrl;
    const seq = ++reqSeq.copilot;
    inFlight.copilot = true;

    state.copilotMessages.push({ role: 'user', text: question });
    renderCopilotMessages();

    setButtonLoading(sendBtn, true, 'Thinking...');
    try {
      const res = await apiRequest('/api/copilot/ask', {
        method: 'POST',
        body: { question: question, provider: provider, api_key: apiKey },
        signal: ctrl ? ctrl.signal : undefined,
      });
      if (seq !== reqSeq.copilot) return;

      if (!res.ok || res.data.ok === false) {
        throw new Error(res.data.error || ('Copilot error (HTTP ' + res.status + ')'));
      }

      const msgObj = {
        role: 'assistant',
        mode: res.data.mode || provider,
        mode_note: res.data.mode_note || '',
        answer_markdown: res.data.answer_markdown || '',
        citations: Array.isArray(res.data.citations) ? res.data.citations : [],
        tool_trace: Array.isArray(res.data.tool_trace) ? res.data.tool_trace : [],
        suggested_followups: Array.isArray(res.data.suggested_followups) ? res.data.suggested_followups : [],
      };
      state.copilotMessages.push(msgObj);
      state.selectedMessageIdx = state.copilotMessages.length - 1;

      renderCopilotMessages();
      renderToolTrace(msgObj);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (seq !== reqSeq.copilot) return;
      showToast('Copilot Error', err.message || 'Unable to get Copilot response.', 'error');
    } finally {
      if (seq === reqSeq.copilot) {
        inFlight.copilot = false;
        abortControllers.copilot = null;
        setButtonLoading(sendBtn, false);
      }
    }
  }

  function renderCopilotMessages(shouldScroll) {
    const autoScroll = shouldScroll !== false;
    const container = $('copilot-messages');
    if (!container) return;
    const prevScrollTop = container.scrollTop;
    container.textContent = '';

    if (state.copilotMessages.length === 0) {
      container.innerHTML =
        '<div class="chat-welcome">' +
          '<div class="chat-welcome-icon" aria-hidden="true">' +
            '<svg width="24" height="24" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.65"><path d="M10 2.5L11.7 7.1L16.5 8.8L11.7 10.5L10 15.1L8.3 10.5L3.5 8.8L8.3 7.1L10 2.5Z"/></svg>' +
          '</div>' +
          '<h3>Grounded AI Financial Analyst</h3>' +
          '<p>Ask any question about budget variance, anomalous vendor charges, duplicate invoices, or SaaS spend optimization. Inspect live SQL &amp; tool traces on the right.</p>' +
        '</div>';
      return;
    }

    state.copilotMessages.forEach(function (msg, idx) {
      if (msg.role === 'user') {
        const div = document.createElement('div');
        div.className = 'chat-bubble chat-bubble-user';
        div.textContent = msg.text;
        container.appendChild(div);
        return;
      }

      const isSelected = idx === state.selectedMessageIdx;
      const card = document.createElement('div');
      card.className = 'chat-bubble chat-bubble-assistant' + (isSelected ? ' is-selected' : '');
      card.tabIndex = 0;
      card.setAttribute('role', 'button');
      card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
      card.setAttribute('aria-label', 'Inspect tool trace for Copilot response');
      card.addEventListener('click', function (ev) {
        if (ev.target.closest('button') || ev.target.closest('a')) return;
        state.selectedMessageIdx = idx;
        renderCopilotMessages(false);
        renderToolTrace(msg);
      });
      card.addEventListener('keydown', function (ev) {
        if (ev.target !== card) return;
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          state.selectedMessageIdx = idx;
          renderCopilotMessages(false);
          renderToolTrace(msg);
        }
      });

      // Header metadata
      const meta = document.createElement('div');
      meta.className = 'chat-assistant-meta';

      const leftMeta = document.createElement('span');
      leftMeta.className = 'chat-meta-left';
      const modePill = document.createElement('span');
      modePill.className = 'pill pill-indigo';
      modePill.textContent = String(msg.mode || 'local');
      leftMeta.appendChild(modePill);
      if (msg.mode_note) {
        const noteSpan = document.createElement('span');
        noteSpan.textContent = msg.mode_note;
        leftMeta.appendChild(noteSpan);
      }

      const traceCount = document.createElement('span');
      traceCount.className = 'tabular chat-trace-count';
      traceCount.textContent = msg.tool_trace.length + ' tool call' + (msg.tool_trace.length === 1 ? '' : 's');

      meta.append(leftMeta, traceCount);
      card.appendChild(meta);

      // Markdown Answer
      const mdDiv = document.createElement('div');
      mdDiv.className = 'markdown-body';
      mdDiv.innerHTML = renderSafeMarkdown(msg.answer_markdown);
      card.appendChild(mdDiv);

      // Verified Citations
      if (msg.citations.length > 0) {
        const citWrap = document.createElement('div');
        citWrap.className = 'chat-citations';
        msg.citations.forEach(function (c) {
          const pill = document.createElement('span');
          pill.className = 'citation-pill';

          const m = document.createElement('span');
          m.className = 'citation-metric';
          m.textContent = (c.metric || 'Metric') + ':';

          const v = document.createElement('span');
          v.className = 'citation-value';
          v.textContent = String(c.value != null ? c.value : '');

          const src = document.createElement('span');
          src.className = 'citation-source';
          src.textContent = '— ' + (c.source || 'SQLite');

          pill.append(m, v, src);
          citWrap.appendChild(pill);
        });
        card.appendChild(citWrap);
      }

      // Clickable follow-ups
      if (msg.suggested_followups.length > 0) {
        const fWrap = document.createElement('div');
        fWrap.className = 'chat-followups';
        msg.suggested_followups.forEach(function (q) {
          const fBtn = document.createElement('button');
          fBtn.type = 'button';
          fBtn.className = 'followup-chip';
          fBtn.textContent = q;
          fBtn.addEventListener('click', function () {
            askCopilot(q);
          });
          fWrap.appendChild(fBtn);
        });
        card.appendChild(fWrap);
      }

      container.appendChild(card);
    });

    if (autoScroll) {
      const bubbles = container.querySelectorAll('.chat-bubble');
      const lastMsg = state.copilotMessages[state.copilotMessages.length - 1];
      if (lastMsg && lastMsg.role === 'assistant' && bubbles.length > 0) {
        const prevIsUser = state.copilotMessages.length >= 2 && state.copilotMessages[state.copilotMessages.length - 2].role === 'user';
        const targetEl = prevIsUser && bubbles.length >= 2
          ? bubbles[bubbles.length - 2]
          : bubbles[bubbles.length - 1];
        const delta = targetEl.getBoundingClientRect().top - container.getBoundingClientRect().top - 4;
        container.scrollTop = Math.max(0, container.scrollTop + delta);
      } else {
        container.scrollTop = container.scrollHeight;
      }
    } else {
      container.scrollTop = prevScrollTop;
    }
  }

  function renderToolTrace(msg) {
    const body = $('copilot-trace-body');
    const stepBadge = $('trace-step-count');
    const subtitle = $('trace-subtitle');
    if (!body) return;

    const steps = msg && Array.isArray(msg.tool_trace) ? msg.tool_trace : [];
    if (stepBadge) stepBadge.textContent = steps.length + ' step' + (steps.length === 1 ? '' : 's');

    if (!msg || steps.length === 0) {
      if (subtitle) subtitle.textContent = 'Deterministic execution audit trail';
      body.innerHTML =
        '<div class="trace-empty">' +
          '<div class="trace-empty-title">Registered Grounded Tools</div>' +
          '<p class="trace-empty-desc">When you ask a question, the agent invokes deterministic tools against SQLite and streams the exact SQL, parameters, latency, and output summary here.</p>' +
          '<ul class="tool-catalog">' +
            '<li><code>query_sql_readonly</code><span>Executes guarded SQL queries</span></li>' +
            '<li><code>compute_department_variance</code><span>Budget, actuals &amp; EAC math</span></li>' +
            '<li><code>detect_anomalies</code><span>Z-score spikes &amp; duplicate audit</span></li>' +
            '<li><code>check_subscriptions</code><span>Price hikes &amp; &gt;90d idle SaaS</span></li>' +
            '<li><code>draft_executive_brief</code><span>Structured FinOps briefing</span></li>' +
          '</ul>' +
        '</div>';
      return;
    }

    const totalMs = steps.reduce(function (acc, s) { return acc + (Number(s.duration_ms) || 0); }, 0);
    if (subtitle) subtitle.textContent = 'Executed ' + steps.length + ' grounded tool calls in ' + formatMs(totalMs);

    body.textContent = '';
    steps.forEach(function (step, i) {
      const card = document.createElement('div');
      card.className = 'trace-step-card';

      const head = document.createElement('div');
      head.className = 'trace-step-head';

      const left = document.createElement('div');
      left.className = 'trace-step-left';

      const num = document.createElement('span');
      num.className = 'trace-step-num tabular';
      num.textContent = String(step.step != null ? step.step : (i + 1));

      const name = document.createElement('span');
      name.className = 'trace-tool-name';
      name.textContent = step.tool || 'tool_call';

      left.append(num, name);

      const dur = document.createElement('span');
      dur.className = 'pill pill-cyan tabular';
      dur.textContent = formatMs(step.duration_ms);

      head.append(left, dur);
      card.appendChild(head);

      if (step.output_summary) {
        const sum = document.createElement('div');
        sum.className = 'trace-summary';
        sum.textContent = step.output_summary;
        card.appendChild(sum);
      }

      if (step.sql) {
        const sqlHead = document.createElement('div');
        sqlHead.className = 'trace-code-header';
        const lbl = document.createElement('span');
        lbl.textContent = 'SQL Executed';
        const openBtn = document.createElement('button');
        openBtn.type = 'button';
        openBtn.className = 'btn btn-xs btn-ghost';
        openBtn.textContent = 'Open in SQL Explorer';
        openBtn.addEventListener('click', function () {
          switchTab('data');
          clearActiveSqlPreset();
          if ($('sql-editor')) $('sql-editor').value = step.sql;
          executeSqlQuery(false);
        });
        sqlHead.append(lbl, openBtn);

        const sqlPre = document.createElement('pre');
        sqlPre.className = 'trace-code-block';
        sqlPre.textContent = step.sql;

        card.append(sqlHead, sqlPre);
      }

      if (step.input && Object.keys(step.input).length > 0) {
        const inHead = document.createElement('div');
        inHead.className = 'trace-code-header';
        inHead.textContent = 'Tool Input JSON';

        const inPre = document.createElement('pre');
        inPre.className = 'trace-code-block';
        inPre.textContent = JSON.stringify(step.input, null, 2);

        card.append(inHead, inPre);
      }

      body.appendChild(card);
    });
  }

  function initCopilotControls() {
    const providerSel = $('copilot-provider');
    const apiKeyInput = $('copilot-api-key');
    if (providerSel) providerSel.addEventListener('change', updateCopilotProviderBadge);
    if (apiKeyInput) apiKeyInput.addEventListener('input', updateCopilotProviderBadge);

    const starterBar = $('copilot-starter-prompts');
    if (starterBar) {
      starterBar.addEventListener('click', function (ev) {
        const chip = ev.target.closest('.prompt-chip');
        if (!chip || inFlight.copilot) return;
        const prompt = chip.dataset.prompt || chip.textContent;
        askCopilot(prompt);
      });
    }

    const form = $('copilot-form');
    const input = $('copilot-input');
    if (form && input) {
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        if (inFlight.copilot) return;
        const text = input.value.trim();
        if (!text) return;
        input.value = '';
        askCopilot(text);
      });

      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' && !ev.shiftKey) {
          ev.preventDefault();
          if (inFlight.copilot) return;
          form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      });
    }

    const clearBtn = $('btn-clear-chat');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        reqSeq.copilot++;
        if (abortControllers.copilot) {
          abortControllers.copilot.abort();
          abortControllers.copilot = null;
        }
        inFlight.copilot = false;
        setButtonLoading($('btn-copilot-send'), false);
        state.copilotMessages = [];
        state.selectedMessageIdx = -1;
        renderCopilotMessages();
        renderToolTrace(null);
      });
    }
  }

  /* ========================================================================
     Tab 4: Automation Workflows
     ======================================================================== */

  const WORKFLOW_PARAM_BOUNDS = {
    variance_threshold_pct: { min: 0.5, max: 50.0, step: 0.5 },
    z_threshold: { min: 1.0, max: 5.0, step: 0.1 },
  };

  async function loadWorkflows(options) {
    const opts = options || {};
    const seq = ++reqSeq.workflow;
    try {
      const res = await apiRequest('/api/workflows');
      if (seq !== reqSeq.workflow) return;
      if (!res.ok) throw new Error(res.data.error || 'Failed to load workflows');
      const incomingWorkflows = res.data.workflows || [];
      state.workflowHistory = res.data.history || [];

      const shouldRenderCards = !opts.preserveCards || state.workflows.length === 0;
      if (shouldRenderCards) {
        state.workflows = incomingWorkflows;
        if ($('nav-badge-workflows')) $('nav-badge-workflows').textContent = String(state.workflows.length || 3);
        renderWorkflowCards();
      }
      renderWorkflowHistory();

      if (!opts.skipAutoSelect && !state.activeWorkflowRun && state.workflowHistory.length > 0) {
        selectWorkflowRun(state.workflowHistory[0]);
      }
    } catch (err) {
      if (seq !== reqSeq.workflow) return;
      showToast('Workflows Error', err.message || 'Could not load automation workflows.', 'error');
    }
  }

  async function refreshWorkflowHistory(seqToken) {
    try {
      const res = await apiRequest('/api/workflows');
      if (seqToken != null && seqToken !== reqSeq.workflow) return;
      if (!res.ok) return;
      state.workflowHistory = res.data.history || [];
      if (state.workflows.length === 0) {
        state.workflows = res.data.workflows || [];
        if ($('nav-badge-workflows')) $('nav-badge-workflows').textContent = String(state.workflows.length || 3);
        renderWorkflowCards();
      }
      renderWorkflowHistory();
    } catch (_err) {
      // Non-fatal history refresh failure; active run output is already rendered
    }
  }

  function renderWorkflowCards() {
    const grid = $('workflow-cards-grid');
    if (!grid) return;
    grid.textContent = '';

    if (state.workflows.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'card empty-state';
      empty.textContent = 'No automation workflows registered.';
      grid.appendChild(empty);
      return;
    }

    state.workflows.forEach(function (wf) {
      const card = document.createElement('article');
      card.className = 'card workflow-card';

      const top = document.createElement('div');
      top.className = 'wf-card-top';

      const titleWrap = document.createElement('div');
      const h3 = document.createElement('h3');
      h3.className = 'wf-card-title';
      h3.textContent = wf.name || wf.id;
      const desc = document.createElement('p');
      desc.className = 'wf-card-desc';
      desc.textContent = wf.description || '';
      titleWrap.append(h3, desc);

      const sched = document.createElement('span');
      sched.className = 'pill pill-indigo';
      sched.textContent = wf.schedule || 'On-Demand';

      top.append(titleWrap, sched);
      card.appendChild(top);

      // Step preview chips
      if (Array.isArray(wf.steps) && wf.steps.length > 0) {
        const stepsWrap = document.createElement('div');
        stepsWrap.className = 'wf-steps-preview';
        wf.steps.forEach(function (st, idx) {
          const tag = document.createElement('span');
          tag.className = 'wf-step-tag';
          tag.textContent = (idx + 1) + '. ' + st;
          stepsWrap.appendChild(tag);
        });
        card.appendChild(stepsWrap);
      }

      // Render ONLY parameters actually supported by this workflow's default_params
      const defaults = Object.assign({}, wf.default_params || {});
      const paramKeys = Object.keys(defaults);
      const inputMap = {};

      if (paramKeys.length > 0) {
        const paramsBox = document.createElement('div');
        paramsBox.className = 'wf-params-grid' + (paramKeys.length === 1 ? ' wf-params-single' : '');

        paramKeys.forEach(function (key) {
          const field = document.createElement('div');
          field.className = 'wf-param-field';

          const lbl = document.createElement('label');
          const inputId = 'wf-param-' + wf.id + '-' + key;
          lbl.setAttribute('for', inputId);
          lbl.textContent = key;

          const inp = document.createElement('input');
          inp.id = inputId;
          const val = defaults[key];
          const isNum = typeof val === 'number';
          const bounds = WORKFLOW_PARAM_BOUNDS[key] || null;
          inp.type = isNum ? 'number' : 'text';
          if (isNum) {
            inp.step = bounds ? String(bounds.step) : (key.indexOf('z_') !== -1 ? '0.1' : '0.5');
            if (bounds) {
              inp.min = String(bounds.min);
              inp.max = String(bounds.max);
            }
          }
          inp.className = 'input-number';
          inp.value = String(val);

          inputMap[key] = {
            el: inp,
            isNumber: isNum,
            defaultVal: val,
            min: bounds ? bounds.min : null,
            max: bounds ? bounds.max : null,
          };
          field.append(lbl, inp);
          paramsBox.appendChild(field);
        });
        card.appendChild(paramsBox);
      }

      const footer = document.createElement('div');
      footer.className = 'wf-card-footer';

      const idSpan = document.createElement('span');
      idSpan.className = 'wf-id-tag';
      idSpan.textContent = wf.id;

      const runBtn = document.createElement('button');
      runBtn.type = 'button';
      runBtn.className = 'btn btn-primary btn-sm';
      runBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M5.5 3.8L16 10L5.5 16.2V3.8Z"/></svg><span>Run Pipeline</span>';
      runBtn.addEventListener('click', function () {
        if (inFlight.workflow) return;
        const params = {};
        const keys = Object.keys(inputMap);
        for (let idx = 0; idx < keys.length; idx++) {
          const k = keys[idx];
          const entry = inputMap[k];
          if (entry.isNumber) {
            const rawStr = entry.el.value.trim();
            const parsed = rawStr === '' ? entry.defaultVal : Number(rawStr);
            if (!Number.isFinite(parsed)) {
              showToast('Invalid Parameter', k + ' must be a valid number.', 'warning');
              entry.el.focus();
              return;
            }
            if (entry.min != null && (parsed < entry.min || parsed > entry.max)) {
              showToast('Parameter Out of Range', k + ' must be between ' + entry.min + ' and ' + entry.max + '.', 'warning');
              entry.el.focus();
              return;
            }
            if (rawStr === '') entry.el.value = String(entry.defaultVal);
            params[k] = parsed;
          } else {
            params[k] = entry.el.value;
          }
        }
        runWorkflowPipeline(wf.id, params, runBtn);
      });

      footer.append(idSpan, runBtn);
      card.appendChild(footer);
      grid.appendChild(card);
    });
  }

  function readMonthlyCloseParams() {
    const defaults = { variance_threshold_pct: 5.0, z_threshold: 2.0 };
    const result = {};
    const keys = Object.keys(defaults);
    for (let i = 0; i < keys.length; i++) {
      const k = keys[i];
      const inp = $('wf-param-monthly_close-' + k);
      if (!inp) {
        result[k] = defaults[k];
        continue;
      }
      const rawStr = inp.value.trim();
      const parsed = rawStr === '' ? defaults[k] : Number(rawStr);
      const bounds = WORKFLOW_PARAM_BOUNDS[k];
      if (!Number.isFinite(parsed) || (bounds && (parsed < bounds.min || parsed > bounds.max))) {
        showToast(
          'Parameter Out of Range',
          k + ' must be between ' + bounds.min + ' and ' + bounds.max + '.',
          'warning'
        );
        inp.focus();
        return null;
      }
      if (rawStr === '') inp.value = String(defaults[k]);
      result[k] = parsed;
    }
    return result;
  }

  async function runWorkflowPipeline(workflowId, params, triggerBtn) {
    if (inFlight.workflow) return;
    inFlight.workflow = true;
    const seq = ++reqSeq.workflow;
    setButtonLoading(triggerBtn, true, 'Running...');
    try {
      const res = await apiRequest('/api/workflows/run', {
        method: 'POST',
        body: { workflow_id: workflowId, params: params || {} },
      });
      if (seq !== reqSeq.workflow) return;

      if (!res.ok || res.data.ok === false) {
        throw new Error(res.data.error || ('Workflow failed (HTTP ' + res.status + ')'));
      }

      const run = res.data.run;
      if (run) {
        selectWorkflowRun(run);
        showToast('Pipeline Completed', (run.workflow_name || workflowId) + ' finished in ' + formatMs(run.duration_ms), 'success');
      }
      // Refresh run history, DB health counts & schema chip counts without rebuilding workflow cards
      await Promise.all([refreshWorkflowHistory(seq), loadHealth(), loadDataSummary()]);
    } catch (err) {
      if (seq !== reqSeq.workflow) return;
      showToast('Workflow Error', err.message || 'Could not execute workflow pipeline.', 'error');
    } finally {
      if (seq === reqSeq.workflow) {
        inFlight.workflow = false;
        setButtonLoading(triggerBtn, false);
      }
    }
  }

  function selectWorkflowRun(run) {
    if (!run) return;
    state.activeWorkflowRun = run;

    $('wf-output-empty').hidden = true;
    $('wf-output-split').hidden = false;
    $('wf-output-actions').hidden = false;

    $('wf-output-title').textContent = (run.workflow_name || run.workflow_id) + ' — Run #' + (run.id != null ? run.id : 'Live');
    $('wf-output-subtitle').textContent = (run.summary || 'Completed') + ' · ' + formatTimestamp(run.ran_at);
    $('wf-output-duration').textContent = formatMs(run.duration_ms);

    // Timeline
    const timeline = $('wf-step-timeline');
    timeline.textContent = '';
    const steps = Array.isArray(run.steps) ? run.steps : [];
    steps.forEach(function (st) {
      const li = document.createElement('li');
      li.className = 'timeline-item';

      const dot = document.createElement('span');
      dot.className = 'timeline-dot';
      dot.textContent = '✓';

      const head = document.createElement('div');
      head.className = 'timeline-head';
      const name = document.createElement('span');
      name.className = 'timeline-name';
      name.textContent = st.name || 'Step';
      const tm = document.createElement('span');
      tm.className = 'timeline-time tabular';
      tm.textContent = formatMs(st.duration_ms);
      head.append(name, tm);

      const det = document.createElement('div');
      det.className = 'timeline-detail';
      det.textContent = st.detail || '';

      li.append(dot, head, det);
      timeline.appendChild(li);
    });

    // Brief + Webhook JSON
    $('wf-brief-view').innerHTML = renderSafeMarkdown(run.output_markdown || 'No executive brief returned.');
    const jsonView = $('wf-json-view');
    jsonView.textContent = '';
    jsonView.appendChild(renderHighlightedJson(run.webhook_payload || {}));

    setWorkflowArtifactTab(state.activeWorkflowArtifactTab);
    renderWorkflowHistory();
  }

  function setWorkflowArtifactTab(view) {
    state.activeWorkflowArtifactTab = view === 'webhook' ? 'webhook' : 'brief';
    const isBrief = state.activeWorkflowArtifactTab === 'brief';

    const briefTab = $('seg-wf-brief');
    const webhookTab = $('seg-wf-webhook');
    if (briefTab) {
      briefTab.classList.toggle('active', isBrief);
      briefTab.setAttribute('aria-selected', isBrief ? 'true' : 'false');
      briefTab.tabIndex = isBrief ? 0 : -1;
    }
    if (webhookTab) {
      webhookTab.classList.toggle('active', !isBrief);
      webhookTab.setAttribute('aria-selected', !isBrief ? 'true' : 'false');
      webhookTab.tabIndex = !isBrief ? 0 : -1;
    }
    if ($('wf-brief-view')) $('wf-brief-view').hidden = !isBrief;
    if ($('wf-json-view')) $('wf-json-view').hidden = isBrief;
    if ($('btn-copy-webhook')) $('btn-copy-webhook').hidden = isBrief;
  }

  function renderWorkflowHistory() {
    const tbody = $('wf-history-body');
    const history = state.workflowHistory || [];
    if ($('wf-history-count')) $('wf-history-count').textContent = history.length + ' run' + (history.length === 1 ? '' : 's');
    if (!tbody) return;
    tbody.textContent = '';

    if (history.length === 0) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 6;
      td.className = 'empty-cell';
      td.textContent = 'No workflow runs recorded yet.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    history.forEach(function (run) {
      const tr = document.createElement('tr');
      const isSelected = state.activeWorkflowRun && state.activeWorkflowRun.id === run.id;
      tr.className = 'clickable-row' + (isSelected ? ' selected-row' : '');
      tr.tabIndex = 0;
      tr.setAttribute('role', 'button');
      tr.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
      tr.setAttribute('aria-label', 'Inspect workflow run #' + (run.id != null ? run.id : '') + ' ' + (run.workflow_name || run.workflow_id || ''));
      tr.addEventListener('click', function () {
        selectWorkflowRun(run);
      });
      tr.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          selectWorkflowRun(run);
        }
      });

      const tdId = document.createElement('td');
      tdId.className = 'mono';
      tdId.textContent = '#' + (run.id != null ? run.id : '—');

      const tdWf = document.createElement('td');
      tdWf.className = 'cell-strong';
      tdWf.textContent = run.workflow_name || run.workflow_id || '—';

      const tdAt = document.createElement('td');
      tdAt.className = 'tabular muted-text';
      tdAt.textContent = formatTimestamp(run.ran_at);

      const tdDur = document.createElement('td');
      tdDur.className = 'num';
      tdDur.textContent = formatMs(run.duration_ms);

      const tdSum = document.createElement('td');
      tdSum.textContent = run.summary || '—';

      const tdSt = document.createElement('td');
      const pill = document.createElement('span');
      pill.className = 'pill ' + (run.status === 'failed' ? 'pill-rose' : 'pill-emerald');
      pill.textContent = run.status || 'completed';
      tdSt.appendChild(pill);

      tr.append(tdId, tdWf, tdAt, tdDur, tdSum, tdSt);
      tbody.appendChild(tr);
    });
  }

  function copyToClipboard(text, label) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        showToast('Copied', label + ' copied to clipboard.', 'success');
      }).catch(function () {
        fallbackCopy(text, label);
      });
    } else {
      fallbackCopy(text, label);
    }
  }

  function fallbackCopy(text, label) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.className = 'sr-only-copy';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showToast('Copied', label + ' copied to clipboard.', 'success');
    } catch (_e) {
      showToast('Copy Failed', 'Browser prevented clipboard access.', 'warning');
    }
    ta.remove();
  }

  function initWorkflowControls() {
    const briefTab = $('seg-wf-brief');
    const webhookTab = $('seg-wf-webhook');
    const segTabs = [briefTab, webhookTab].filter(Boolean);

    if (briefTab) {
      briefTab.addEventListener('click', function () { setWorkflowArtifactTab('brief'); });
    }
    if (webhookTab) {
      webhookTab.addEventListener('click', function () { setWorkflowArtifactTab('webhook'); });
    }
    segTabs.forEach(function (tab, idx) {
      tab.addEventListener('keydown', function (ev) {
        let nextIdx = -1;
        if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') {
          nextIdx = (idx + 1) % segTabs.length;
        } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') {
          nextIdx = (idx - 1 + segTabs.length) % segTabs.length;
        } else if (ev.key === 'Home') {
          nextIdx = 0;
        } else if (ev.key === 'End') {
          nextIdx = segTabs.length - 1;
        }
        if (nextIdx !== -1) {
          ev.preventDefault();
          const targetTab = segTabs[nextIdx];
          setWorkflowArtifactTab(targetTab.dataset.wfView);
          targetTab.focus();
        }
      });
    });

    if ($('btn-copy-brief')) {
      $('btn-copy-brief').addEventListener('click', function () {
        if (state.activeWorkflowRun && state.activeWorkflowRun.output_markdown) {
          copyToClipboard(state.activeWorkflowRun.output_markdown, 'Executive brief markdown');
        }
      });
    }

    if ($('btn-download-brief')) {
      $('btn-download-brief').addEventListener('click', function () {
        if (state.activeWorkflowRun && state.activeWorkflowRun.output_markdown) {
          const fname = (state.activeWorkflowRun.workflow_id || 'pulseops') + '_brief.md';
          downloadTextFile(fname, state.activeWorkflowRun.output_markdown, 'text/markdown;charset=utf-8');
          showToast('Downloaded', fname + ' saved.', 'success');
        }
      });
    }

    if ($('btn-copy-webhook')) {
      $('btn-copy-webhook').addEventListener('click', function () {
        if (state.activeWorkflowRun && state.activeWorkflowRun.webhook_payload) {
          copyToClipboard(JSON.stringify(state.activeWorkflowRun.webhook_payload, null, 2), 'Webhook JSON payload');
        }
      });
    }

    // Header 1-click "Run Monthly Close" CTA (aligned to catalog default z_threshold: 2.0 or current card inputs)
    const topCta = $('btn-run-monthly-close');
    if (topCta) {
      topCta.addEventListener('click', async function () {
        if (inFlight.workflow) return;
        switchTab('workflows', false, true);
        const params = readMonthlyCloseParams();
        if (params === null) return;
        await runWorkflowPipeline('monthly_close', params, topCta);
      });
    }
  }

  /* ========================================================================
     Tab 5: AI Evals & Reliability
     ======================================================================== */

  async function loadLatestEvals() {
    const seq = ++reqSeq.evals;
    try {
      const res = await apiRequest('/api/evals/latest');
      if (seq !== reqSeq.evals) return;
      if (!res.ok) {
        state.evals = null;
        renderEvals(null);
        return;
      }
      const d = res.data;
      if (d && d.has_run !== false && (Number(d.total) > 0 || (Array.isArray(d.cases) && d.cases.length > 0))) {
        state.evals = d;
        renderEvals(d);
      } else {
        state.evals = null;
        renderEvals(null);
      }
    } catch (_err) {
      if (seq !== reqSeq.evals) return;
      state.evals = null;
      renderEvals(null);
    }
  }

  async function runEvalSuite() {
    if (inFlight.evals) return;
    inFlight.evals = true;
    const seq = ++reqSeq.evals;
    const btn = $('btn-run-evals');
    setButtonLoading(btn, true, 'Running Evals...');
    try {
      const res = await apiRequest('/api/evals/run', { method: 'POST', body: {} });
      if (seq !== reqSeq.evals) return;
      if (!res.ok || res.data.ok === false) {
        throw new Error(res.data.error || ('Eval run failed (HTTP ' + res.status + ')'));
      }
      state.evals = res.data;
      state.evalCategoryFilter = null;
      renderEvals(res.data);

      const pct = normalizeEvalPct(res.data.pass_rate, res.data.passed, res.data.total);
      updateEvalIndicators(pct, res.data.passed, res.data.total);
      showToast(
        'Eval Suite Complete',
        res.data.passed + '/' + res.data.total + ' cases passed (' + Math.round(pct || 0) + '%)',
        pct >= 90 ? 'success' : 'warning'
      );
      await Promise.all([loadHealth(), loadDataSummary()]);
    } catch (err) {
      if (seq !== reqSeq.evals) return;
      showToast('Eval Run Failed', err.message || 'Could not execute eval harness.', 'error');
    } finally {
      if (seq === reqSeq.evals) {
        inFlight.evals = false;
        setButtonLoading(btn, false);
      }
    }
  }

  function renderEvals(data) {
    const gaugeBox = $('chart-eval-gauge');
    const catGrid = $('eval-category-grid');
    const tbody = $('eval-cases-body');
    const clearBtn = $('btn-clear-eval-filter');

    if (!data || data.has_run === false || !Array.isArray(data.cases) || data.cases.length === 0) {
      state.evalCategoryFilter = null;
      if (window.PulseCharts) window.PulseCharts.renderEvalGauge(gaugeBox, null, 0, 0);
      updateEvalIndicators(null, null, null);
      if ($('eval-stat-passed')) $('eval-stat-passed').textContent = '-- / --';
      if ($('eval-stat-latency')) $('eval-stat-latency').textContent = '-- ms';
      if ($('eval-stat-ran-at')) $('eval-stat-ran-at').textContent = 'Not run yet';
      if (catGrid) catGrid.textContent = '';
      if (clearBtn) clearBtn.hidden = true;
      if ($('eval-cases-count')) $('eval-cases-count').textContent = '0 cases';
      if ($('eval-cases-subtitle')) {
        $('eval-cases-subtitle').textContent = 'Live assertion outcomes against ground-truth SQLite state';
      }
      if (tbody) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">Click <strong>Run Eval Suite</strong> above to run all deterministic verification assertions.</td></tr>';
      }
      return;
    }

    const pct = normalizeEvalPct(data.pass_rate, data.passed, data.total);
    if (window.PulseCharts) {
      window.PulseCharts.renderEvalGauge(gaugeBox, pct, data.passed, data.total);
    }
    updateEvalIndicators(pct, data.passed, data.total);

    $('eval-stat-passed').textContent = data.passed + ' / ' + data.total;
    $('eval-stat-latency').textContent = formatMs(data.avg_latency_ms);
    $('eval-stat-ran-at').textContent = formatTimestamp(data.ran_at);

    // Category Breakdown Pills
    if (catGrid) {
      catGrid.textContent = '';
      const cats = data.categories || {};
      Object.keys(cats).forEach(function (catName) {
        const c = cats[catName] || { passed: 0, total: 0 };
        const ratio = c.total > 0 ? (c.passed / c.total) * 100 : 0;
        const isActive = state.evalCategoryFilter === catName;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'eval-cat-pill' + (isActive ? ' active' : '');
        btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        btn.addEventListener('click', function () {
          state.evalCategoryFilter = state.evalCategoryFilter === catName ? null : catName;
          renderEvals(state.evals);
        });

        const top = document.createElement('div');
        top.className = 'eval-cat-top';

        const nameEl = document.createElement('span');
        nameEl.className = 'eval-cat-name';
        nameEl.textContent = catName;

        const scoreEl = document.createElement('span');
        scoreEl.className = 'pill tabular ' + (ratio === 100 ? 'pill-emerald' : ratio >= 70 ? 'pill-amber' : 'pill-rose');
        scoreEl.textContent = c.passed + '/' + c.total;

        top.append(nameEl, scoreEl);

        const bar = document.createElement('div');
        bar.className = 'eval-cat-bar';
        const fill = document.createElement('div');
        fill.className = 'eval-cat-bar-fill' + (ratio === 100 ? '' : ratio >= 70 ? ' is-warn' : ' is-fail');
        fill.style.width = ratio.toFixed(0) + '%';
        bar.appendChild(fill);

        btn.append(top, bar);
        catGrid.appendChild(btn);
      });
    }

    // Cases Table
    if (clearBtn) clearBtn.hidden = !state.evalCategoryFilter;

    const filteredCases = data.cases.filter(function (tc) {
      return !state.evalCategoryFilter || tc.category === state.evalCategoryFilter;
    });

    if ($('eval-cases-count')) {
      $('eval-cases-count').textContent = filteredCases.length + ' case' + (filteredCases.length === 1 ? '' : 's');
    }
    if ($('eval-cases-subtitle')) {
      $('eval-cases-subtitle').textContent = state.evalCategoryFilter
        ? ('Showing category: ' + state.evalCategoryFilter)
        : 'Live assertion outcomes against ground-truth SQLite state';
    }

    tbody.textContent = '';
    filteredCases.forEach(function (tc) {
      const tr = document.createElement('tr');

      const tdId = document.createElement('td');
      tdId.className = 'mono muted-text nowrap';
      tdId.textContent = tc.id || '—';

      const tdCat = document.createElement('td');
      const catPill = document.createElement('span');
      catPill.className = 'pill pill-neutral';
      catPill.textContent = tc.category || 'General';
      tdCat.appendChild(catPill);

      const tdName = document.createElement('td');
      const nm = document.createElement('span');
      nm.className = 'cell-strong';
      nm.textContent = tc.name || '—';
      tdName.appendChild(nm);
      if (tc.detail) {
        const det = document.createElement('span');
        det.className = 'cell-sub';
        det.textContent = tc.detail;
        tdName.appendChild(det);
      }

      const tdExp = document.createElement('td');
      tdExp.className = 'mono-sm';
      tdExp.textContent = tc.expected || '—';

      const tdAct = document.createElement('td');
      tdAct.className = 'mono-sm';
      tdAct.textContent = tc.actual || '—';

      const tdLat = document.createElement('td');
      tdLat.className = 'num nowrap';
      tdLat.textContent = formatMs(tc.latency_ms);

      const tdRes = document.createElement('td');
      const resPill = document.createElement('span');
      resPill.className = 'pill ' + (tc.passed ? 'pill-emerald' : 'pill-rose');
      resPill.textContent = tc.passed ? 'PASS' : 'FAIL';
      tdRes.appendChild(resPill);

      tr.append(tdId, tdCat, tdName, tdExp, tdAct, tdLat, tdRes);
      tbody.appendChild(tr);
    });
  }

  function initEvalControls() {
    if ($('btn-run-evals')) {
      $('btn-run-evals').addEventListener('click', runEvalSuite);
    }
    if ($('btn-clear-eval-filter')) {
      $('btn-clear-eval-filter').addEventListener('click', function () {
        state.evalCategoryFilter = null;
        renderEvals(state.evals);
      });
    }
  }

  /* ========================================================================
     Bootstrap
     ======================================================================== */

  async function init() {
    initTheme();
    initNavigation();
    initOverviewControls();
    initDataTabControls();
    initCopilotControls();
    initWorkflowControls();
    initEvalControls();

    if ($('btn-reset-demo')) {
      $('btn-reset-demo').addEventListener('click', handleResetDemoData);
    }

    const initialHash = (window.location.hash || '').replace(/^#/, '');
    const startTab = TAB_META[initialHash] ? initialHash : 'overview';
    switchTab(startTab, true, true);

    // Hydrate read-only evals first so overview indicators never race, then load active views
    await loadLatestEvals();
    const bootCalls = [loadHealth(), loadOverview()];
    if (startTab === 'data') bootCalls.push(loadDataSummary());
    if (startTab === 'workflows') bootCalls.push(loadWorkflows());
    await Promise.all(bootCalls);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
