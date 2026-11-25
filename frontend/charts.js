/* ==========================================================================
   PulseOps AI — Zero-Dependency Custom SVG Data Visualizations
   Exports window.PulseCharts
   ========================================================================== */

(function (global) {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs, text) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (attrs[key] !== undefined && attrs[key] !== null) {
          el.setAttribute(key, String(attrs[key]));
        }
      });
    }
    if (text !== undefined && text !== null) {
      el.textContent = String(text);
    }
    return el;
  }

  function formatCompactCurrency(val) {
    const num = Number(val) || 0;
    const abs = Math.abs(num);
    const sign = num < 0 ? '-' : '';
    if (abs >= 1000000) {
      const m = abs / 1000000;
      const mStr = Math.abs(m - Math.round(m)) < 0.001
        ? String(Math.round(m))
        : m.toFixed(1).replace(/\.0$/, '');
      return sign + '$' + mStr + 'M';
    }
    if (abs >= 1000) {
      const k = abs / 1000;
      const kStr = Math.abs(k - Math.round(k)) < 0.001
        ? String(Math.round(k))
        : k.toFixed(1).replace(/\.0$/, '');
      return sign + '$' + kStr + 'k';
    }
    return sign + '$' + Math.round(abs);
  }

  function formatFullCurrency(val) {
    const num = Number(val) || 0;
    return (num < 0 ? '-' : '') + '$' + Math.abs(num).toLocaleString('en-US', {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    });
  }

  function formatMonthLabel(raw, showYear) {
    const str = String(raw || '');
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const match = str.match(/^(\d{4})-(\d{2})/);
    if (match) {
      const idx = parseInt(match[2], 10) - 1;
      if (idx >= 0 && idx < 12) {
        return showYear ? (months[idx] + " '" + match[1].slice(2)) : months[idx];
      }
    }
    return str.length > 7 ? str.slice(0, 7) : str;
  }

  function truncateLabel(text, maxLen) {
    const str = String(text || '');
    if (str.length <= maxLen) return str;
    return str.slice(0, Math.max(1, maxLen - 1)) + '…';
  }

  function getTooltipEl() {
    return document.getElementById('chart-tooltip');
  }

  function showTooltip(event, title, rows) {
    const tip = getTooltipEl();
    if (!tip) return;
    tip.textContent = '';

    const titleDiv = document.createElement('div');
    titleDiv.className = 'tooltip-title';
    titleDiv.textContent = title;
    tip.appendChild(titleDiv);

    rows.forEach(function (row) {
      const r = document.createElement('div');
      r.className = 'tooltip-row';

      const lbl = document.createElement('span');
      lbl.className = 'tooltip-label';
      if (row.color) {
        const dot = document.createElement('span');
        dot.className = 'tooltip-swatch';
        dot.style.background = row.color;
        lbl.appendChild(dot);
      }
      const txt = document.createElement('span');
      txt.textContent = row.label;
      lbl.appendChild(txt);

      const val = document.createElement('span');
      val.className = 'tooltip-val';
      if (row.valueClass) val.classList.add(row.valueClass);
      val.textContent = row.value;

      r.appendChild(lbl);
      r.appendChild(val);
      tip.appendChild(r);
    });

    // Reset previous position before measuring to prevent stale rect distortion
    tip.style.left = '0px';
    tip.style.top = '0px';
    tip.hidden = false;
    positionTooltip(event, tip);
  }

  function positionTooltip(event, tip) {
    if (!tip || tip.hidden) return;
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let x = event.clientX + pad;
    let y = event.clientY - rect.height / 2;
    if (x + rect.width > window.innerWidth - 12) {
      x = event.clientX - rect.width - pad;
    }
    x = Math.max(10, Math.min(x, window.innerWidth - rect.width - 10));
    y = Math.max(10, Math.min(y, window.innerHeight - rect.height - 10));
    tip.style.left = Math.round(x) + 'px';
    tip.style.top = Math.round(y) + 'px';
  }

  function hideTooltip() {
    const tip = getTooltipEl();
    if (tip) tip.hidden = true;
  }

  function computeNiceScale(rawMax) {
    if (!rawMax || rawMax <= 0) {
      return { yMax: 100000, ticks: 4, step: 25000 };
    }
    const targetMax = rawMax * 1.08;
    const roughStep = targetMax / 5;
    const exp = Math.pow(10, Math.floor(Math.log10(roughStep)));
    const candidates = [1, 2, 2.5, 5, 10];

    let bestStep = candidates[candidates.length - 1] * exp;
    let bestTicks = Math.ceil(targetMax / bestStep);

    for (let i = 0; i < candidates.length; i++) {
      const step = candidates[i] * exp;
      const ticks = Math.ceil(targetMax / step);
      if (ticks >= 4 && ticks <= 5) {
        bestStep = step;
        bestTicks = ticks;
        break;
      }
      if (ticks >= 3 && ticks <= 6) {
        bestStep = step;
        bestTicks = ticks;
      }
    }

    return {
      yMax: bestStep * bestTicks,
      ticks: bestTicks,
      step: bestStep,
    };
  }

  /**
   * 1. Monthly Budget vs. Actual Spend Grouped Bar + Trend Line Chart
   */
  function renderMonthlyBudgetActualChart(container, data) {
    if (!container) return;
    container.textContent = '';

    if (!Array.isArray(data) || data.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'chart-skeleton';
      empty.textContent = 'No monthly spend data available.';
      container.appendChild(empty);
      return;
    }

    const W = 640;
    const H = 262;
    const pad = { top: 18, right: 16, bottom: 32, left: 54 };
    const plotW = W - pad.left - pad.right;
    const plotH = H - pad.top - pad.bottom;

    let rawMax = 0;
    const yearsSeen = {};
    data.forEach(function (d) {
      rawMax = Math.max(rawMax, Number(d.budget) || 0, Number(d.actual) || 0);
      const ym = String(d.month || '').match(/^(\d{4})-/);
      if (ym) yearsSeen[ym[1]] = true;
    });
    const isMultiYear = Object.keys(yearsSeen).length > 1;
    const scale = computeNiceScale(rawMax);
    const yMax = scale.yMax;
    const ticks = scale.ticks;

    const svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      role: 'img',
      'aria-label': 'Monthly Budget vs Actual Spend Chart',
    });

    // Subtle trend area gradient
    const defs = svgEl('defs');
    const grad = svgEl('linearGradient', {
      id: 'pulseTrendAreaGrad',
      x1: '0',
      y1: '0',
      x2: '0',
      y2: '1',
    });
    grad.appendChild(svgEl('stop', { offset: '0%', 'stop-color': 'var(--chart-trend)', 'stop-opacity': '0.18' }));
    grad.appendChild(svgEl('stop', { offset: '100%', 'stop-color': 'var(--chart-trend)', 'stop-opacity': '0.0' }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    function yPos(v) {
      return pad.top + plotH - (Math.max(0, Number(v) || 0) / yMax) * plotH;
    }

    // Horizontal grid lines + rounded Y labels
    const gridGroup = svgEl('g');
    for (let i = 0; i <= ticks; i++) {
      const val = scale.step * i;
      const y = yPos(val);
      gridGroup.appendChild(svgEl('line', {
        x1: pad.left,
        y1: y,
        x2: W - pad.right,
        y2: y,
        stroke: i === 0 ? 'var(--chart-axis)' : 'var(--chart-grid)',
        'stroke-width': i === 0 ? 1.1 : 1,
        'stroke-dasharray': i === 0 ? null : '3 3',
      }));

      gridGroup.appendChild(svgEl('text', {
        x: pad.left - 8,
        y: y + 3.5,
        'text-anchor': 'end',
        fill: 'var(--chart-text)',
        'font-size': '10.5',
        'font-family': 'var(--font-sans)',
      }, formatCompactCurrency(val)));
    }
    svg.appendChild(gridGroup);

    const n = data.length;
    const slotW = plotW / n;
    const barGap = slotW < 18 ? 1 : 3;
    const barW = Math.max(2, Math.min(17, (slotW * 0.66 - barGap) / 2));
    const labelStep = n > 14 ? Math.ceil(n / 12) : 1;

    const trendPoints = [];
    const barsGroup = svgEl('g');
    const hoverGroup = svgEl('g');

    data.forEach(function (d, i) {
      const cx = pad.left + slotW * (i + 0.5);
      const budget = Number(d.budget) || 0;
      const actual = Number(d.actual) || 0;
      const hasBudget = budget > 0;
      const isOver = hasBudget ? (actual > budget * 1.001) : (actual > 0);

      const bY = yPos(budget);
      const bH = budget > 0 ? Math.max(1, pad.top + plotH - bY) : 0;
      const aY = yPos(actual);
      const aH = actual > 0 ? Math.max(1, pad.top + plotH - aY) : 0;

      const bX = cx - barW - barGap / 2;
      const aX = cx + barGap / 2;

      // Hover highlight backdrop
      const highlightRect = svgEl('rect', {
        x: pad.left + slotW * i + 1,
        y: pad.top,
        width: Math.max(3, slotW - 2),
        height: plotH,
        rx: 4,
        fill: 'var(--indigo)',
        'fill-opacity': '0',
      });
      barsGroup.appendChild(highlightRect);

      // Budget bar
      if (bH > 0) {
        barsGroup.appendChild(svgEl('rect', {
          x: bX,
          y: bY,
          width: barW,
          height: bH,
          rx: Math.min(2.5, barW / 2),
          fill: 'var(--chart-budget)',
          opacity: '0.92',
        }));
      }

      // Actual bar
      if (aH > 0) {
        barsGroup.appendChild(svgEl('rect', {
          x: aX,
          y: aY,
          width: barW,
          height: aH,
          rx: Math.min(2.5, barW / 2),
          fill: isOver ? 'var(--chart-over)' : 'var(--chart-actual)',
        }));
      }

      // Month X label (skip intermediate labels on dense multi-year timelines)
      if (i % labelStep === 0 || i === n - 1) {
        const showYr = isMultiYear && (i === 0 || String(d.month || '').slice(5, 7) === '01');
        barsGroup.appendChild(svgEl('text', {
          x: cx,
          y: H - 10,
          'text-anchor': 'middle',
          fill: 'var(--chart-text)',
          'font-size': isMultiYear ? '9.5' : '10.5',
          'font-family': 'var(--font-sans)',
        }, formatMonthLabel(d.month, showYr)));
      }

      trendPoints.push({ x: cx, y: aY, d: d, isOver: isOver });

      // Interactive hitbox column
      const hit = svgEl('rect', {
        x: pad.left + slotW * i,
        y: pad.top,
        width: slotW,
        height: plotH + 22,
        fill: 'transparent',
        class: 'chart-hit-col',
      });

      const delta = actual - budget;
      const deltaPrefix = delta > 0 ? '+' : '';
      let varianceRow;
      if (!hasBudget) {
        varianceRow = {
          label: actual > 0 ? 'Unbudgeted' : 'Variance',
          value: (actual > 0 ? '+' : '') + formatFullCurrency(delta) + ' (N/A)',
          valueClass: actual > 0 ? 'text-rose' : '',
        };
      } else {
        const pct = (delta / budget) * 100;
        varianceRow = {
          label: delta > 0 ? 'Over Plan' : 'Under Plan',
          value: deltaPrefix + formatFullCurrency(delta) + ' (' + deltaPrefix + pct.toFixed(1) + '%)',
          valueClass: delta > 0 ? 'text-rose' : 'text-emerald',
        };
      }

      hit.addEventListener('mouseenter', function (ev) {
        highlightRect.setAttribute('fill-opacity', '0.07');
        showTooltip(ev, String(d.month), [
          { label: 'Budget', value: formatFullCurrency(budget), color: 'var(--chart-budget)' },
          { label: 'Actual Spend', value: formatFullCurrency(actual), color: isOver ? 'var(--chart-over)' : 'var(--chart-actual)' },
          varianceRow,
        ]);
      });

      hit.addEventListener('mousemove', function (ev) {
        positionTooltip(ev, getTooltipEl());
      });

      hit.addEventListener('mouseleave', function () {
        highlightRect.setAttribute('fill-opacity', '0');
        hideTooltip();
      });

      hoverGroup.appendChild(hit);
    });

    svg.appendChild(barsGroup);

    // Monthly actual spend trend line + subtle gradient area
    if (trendPoints.length > 1) {
      const lineD = trendPoints.map(function (pt, idx) {
        return (idx === 0 ? 'M ' : 'L ') + pt.x.toFixed(1) + ' ' + pt.y.toFixed(1);
      }).join(' ');

      const areaD = lineD +
        ' L ' + trendPoints[trendPoints.length - 1].x.toFixed(1) + ' ' + (pad.top + plotH).toFixed(1) +
        ' L ' + trendPoints[0].x.toFixed(1) + ' ' + (pad.top + plotH).toFixed(1) + ' Z';

      svg.appendChild(svgEl('path', {
        d: areaD,
        fill: 'url(#pulseTrendAreaGrad)',
        'pointer-events': 'none',
      }));

      svg.appendChild(svgEl('path', {
        d: lineD,
        fill: 'none',
        stroke: 'var(--chart-trend)',
        'stroke-width': '2',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        'pointer-events': 'none',
      }));

      const nodesGroup = svgEl('g', { 'pointer-events': 'none' });
      const nodeRadius = n > 24 ? 2.2 : 3.1;
      trendPoints.forEach(function (pt) {
        nodesGroup.appendChild(svgEl('circle', {
          cx: pt.x,
          cy: pt.y,
          r: nodeRadius,
          fill: 'var(--bg-surface)',
          stroke: pt.isOver ? 'var(--chart-over)' : 'var(--chart-trend)',
          'stroke-width': '2',
        }));
      });
      svg.appendChild(nodesGroup);
    }

    svg.appendChild(hoverGroup);
    container.appendChild(svg);
  }

  /**
   * 2. Department Variance Horizontal Diverging Bar Chart
   */
  function renderDepartmentVarianceChart(container, data, options) {
    if (!container) return;
    container.textContent = '';

    if (!Array.isArray(data) || data.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'chart-skeleton';
      empty.textContent = 'No department variance data.';
      container.appendChild(empty);
      return;
    }

    const opts = options || {};
    const selectedDept = opts.selectedDepartment || null;
    const onSelect = typeof opts.onSelectDepartment === 'function' ? opts.onSelectDepartment : null;

    const rowH = 31;
    const pad = { top: 10, right: 74, bottom: 20, left: 108 };
    const W = 540;
    const H = pad.top + pad.bottom + data.length * rowH;
    const plotW = W - pad.left - pad.right;

    // Compute signed variance % directly from actual vs budget for guaranteed visual consistency
    const rows = data.map(function (d) {
      const budget = Number(d.budget) || 0;
      const actual = Number(d.actual) || 0;
      const diff = actual - budget;
      const hasBudget = budget > 0;
      const pct = hasBudget ? (diff / budget) * 100 : null;
      const fallbackStatus = !hasBudget
        ? (actual > 0 ? 'over_budget' : 'on_track')
        : (pct > 5 ? 'over_budget' : pct > 0 ? 'watch' : 'on_track');
      return {
        raw: d,
        department: String(d.department || 'Unknown'),
        budget: budget,
        actual: actual,
        diff: diff,
        hasBudget: hasBudget,
        pct: pct,
        eac: Number(d.eac) || actual,
        status: String(d.status || fallbackStatus),
      };
    });

    let maxAbsPct = 10;
    rows.forEach(function (r) {
      if (r.hasBudget && r.pct !== null) {
        maxAbsPct = Math.max(maxAbsPct, Math.abs(r.pct));
      }
    });
    const bound = Math.ceil((maxAbsPct * 1.15) / 5) * 5;
    const zeroX = pad.left + plotW * 0.45;
    const leftSpan = zeroX - pad.left;
    const rightSpan = pad.left + plotW - zeroX;

    const svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      role: 'img',
      'aria-label': 'Department Variance Percentage Bar Chart',
    });

    // Zero baseline
    svg.appendChild(svgEl('line', {
      x1: zeroX,
      y1: pad.top - 4,
      x2: zeroX,
      y2: H - pad.bottom + 2,
      stroke: 'var(--chart-axis)',
      'stroke-width': '1.2',
    }));

    // Axis captions
    svg.appendChild(svgEl('text', {
      x: pad.left,
      y: H - 5,
      'text-anchor': 'start',
      fill: 'var(--chart-text)',
      'font-size': '9.5',
    }, '← Under Budget'));
    svg.appendChild(svgEl('text', {
      x: zeroX,
      y: H - 5,
      'text-anchor': 'middle',
      fill: 'var(--chart-text)',
      'font-size': '9.5',
      'font-weight': '600',
    }, '0%'));
    svg.appendChild(svgEl('text', {
      x: pad.left + plotW,
      y: H - 5,
      'text-anchor': 'end',
      fill: 'var(--chart-text)',
      'font-size': '9.5',
    }, 'Over Budget →'));

    rows.forEach(function (r, idx) {
      const yMid = pad.top + idx * rowH + rowH / 2;
      const barH = 15;
      const isSelected = selectedDept && selectedDept === r.department;
      const pctText = r.hasBudget
        ? ((r.pct > 0 ? '+' : '') + r.pct.toFixed(1) + '%')
        : (r.actual > 0 ? 'Unbudg.' : 'N/A');

      const g = svgEl('g', {
        class: 'chart-dept-row',
        tabindex: '0',
        role: 'button',
        'aria-pressed': isSelected ? 'true' : 'false',
        'aria-label': r.department + ' department variance ' + pctText + ', activate to filter anomalies and subscriptions',
      });

      g.appendChild(svgEl('title', null, r.department + ': ' + pctText));

      // Row hover/selection backdrop
      const rowBg = svgEl('rect', {
        x: 2,
        y: pad.top + idx * rowH + 2,
        width: W - 4,
        height: rowH - 4,
        rx: 5,
        fill: 'var(--indigo)',
        'fill-opacity': isSelected ? '0.14' : '0',
      });
      g.appendChild(rowBg);

      // Track line
      g.appendChild(svgEl('rect', {
        x: pad.left,
        y: yMid - barH / 2,
        width: plotW,
        height: barH,
        rx: 3,
        fill: 'var(--chart-track)',
      }));

      // Department label (truncated to stay cleanly inside pad.left)
      g.appendChild(svgEl('text', {
        x: pad.left - 10,
        y: yMid + 4,
        'text-anchor': 'end',
        fill: isSelected ? 'var(--text-primary)' : 'var(--chart-text)',
        'font-size': '11.5',
        'font-weight': isSelected ? '700' : '600',
      }, truncateLabel(r.department, 13)));

      // Bar fill color by status
      let barFill = 'var(--chart-under)';
      if (!r.hasBudget) {
        barFill = r.actual > 0 ? 'var(--chart-over)' : 'var(--chart-axis)';
      } else if (r.status === 'over_budget' || r.pct > 5) {
        barFill = 'var(--chart-over)';
      } else if (r.status === 'watch' || r.pct > 0) {
        barFill = 'var(--chart-watch)';
      }

      if (!r.hasBudget) {
        if (r.actual > 0) {
          g.appendChild(svgEl('rect', {
            x: zeroX,
            y: yMid - barH / 2,
            width: rightSpan,
            height: barH,
            rx: 3,
            fill: 'var(--chart-over)',
          }));
        }
      } else if (Math.abs(r.pct) < 0.05) {
        // Explicit 0.0% marker centered on zero baseline
        g.appendChild(svgEl('rect', {
          x: zeroX - 1.5,
          y: yMid - barH / 2,
          width: 3,
          height: barH,
          rx: 1.5,
          fill: 'var(--chart-axis)',
        }));
      } else {
        const ratio = Math.min(1, Math.abs(r.pct) / bound);
        let barX = zeroX;
        let barW = 3;
        if (r.pct > 0) {
          barW = Math.max(3, ratio * rightSpan);
          barX = zeroX;
        } else {
          barW = Math.max(3, ratio * leftSpan);
          barX = zeroX - barW;
        }
        g.appendChild(svgEl('rect', {
          x: barX,
          y: yMid - barH / 2,
          width: barW,
          height: barH,
          rx: 3,
          fill: barFill,
        }));
      }

      // Value text on right edge
      let valFill = 'var(--chart-under)';
      if (!r.hasBudget) {
        valFill = r.actual > 0 ? 'var(--chart-over)' : 'var(--text-muted)';
      } else if (r.pct > 0) {
        valFill = r.status === 'over_budget' ? 'var(--chart-over)' : 'var(--chart-watch)';
      } else if (Math.abs(r.pct) < 0.05) {
        valFill = 'var(--text-secondary)';
      }

      g.appendChild(svgEl('text', {
        x: W - 8,
        y: yMid + 4,
        'text-anchor': 'end',
        fill: valFill,
        'font-size': '11',
        'font-weight': '700',
        'font-family': 'var(--font-mono)',
      }, r.hasBudget ? pctText : 'N/A'));

      const tooltipVarianceText = r.hasBudget
        ? ((r.diff > 0 ? '+' : '') + formatFullCurrency(r.diff) + ' (' + pctText + ')')
        : ((r.diff > 0 ? '+' : '') + formatFullCurrency(r.diff) + ' (N/A)');

      g.addEventListener('mouseenter', function (ev) {
        if (!isSelected) rowBg.setAttribute('fill-opacity', '0.07');
        showTooltip(ev, r.department + ' Department', [
          { label: 'Budget', value: formatFullCurrency(r.budget), color: 'var(--chart-budget)' },
          { label: 'Actual Spend', value: formatFullCurrency(r.actual), color: barFill },
          {
            label: 'Variance',
            value: tooltipVarianceText,
            valueClass: r.diff > 0 ? 'text-rose' : r.diff < 0 ? 'text-emerald' : '',
          },
          { label: 'Projected EAC', value: formatFullCurrency(r.eac) },
        ]);
      });

      g.addEventListener('mousemove', function (ev) {
        positionTooltip(ev, getTooltipEl());
      });

      g.addEventListener('mouseleave', function () {
        if (!isSelected) rowBg.setAttribute('fill-opacity', '0');
        hideTooltip();
      });

      g.addEventListener('focus', function () {
        if (!isSelected) rowBg.setAttribute('fill-opacity', '0.08');
      });

      g.addEventListener('blur', function () {
        if (!isSelected) rowBg.setAttribute('fill-opacity', '0');
      });

      g.addEventListener('click', function () {
        if (onSelect) onSelect(r.department);
      });

      g.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          if (onSelect) onSelect(r.department);
        }
      });

      svg.appendChild(g);
    });

    container.appendChild(svg);
  }

  /**
   * 3. AI Eval Pass Rate Radial Donut Gauge SVG
   */
  function renderEvalGauge(container, passRate, passed, total) {
    if (!container) return;
    container.textContent = '';

    const size = 132;
    const cx = size / 2;
    const cy = size / 2;
    const r = 52;
    const strokeW = 10;
    const circ = 2 * Math.PI * r;

    const totalNum = total !== null && total !== undefined ? Number(total) : 0;
    const passedNum = passed !== null && passed !== undefined ? Number(passed) : 0;
    const hasCounts = Number.isFinite(totalNum) && totalNum > 0 && Number.isFinite(passedNum);
    const hasRate = passRate !== null && passRate !== undefined && passRate !== '' && Number.isFinite(Number(passRate));
    const hasValue = hasCounts || hasRate;

    // Treat pass_rate strictly as 0..100 (or derive directly from passed/total)
    let pct = 0;
    if (hasCounts) {
      pct = (passedNum / totalNum) * 100;
    } else if (hasRate) {
      pct = Number(passRate);
    }
    pct = Math.max(0, Math.min(100, pct));

    let ringColor = 'var(--text-muted)';
    if (hasValue) {
      if (pct >= 90) ringColor = 'var(--emerald)';
      else if (pct >= 70) ringColor = 'var(--amber)';
      else ringColor = 'var(--rose)';
    }

    const svg = svgEl('svg', {
      viewBox: '0 0 ' + size + ' ' + size,
      width: size,
      height: size,
      role: 'img',
      'aria-label': hasValue ? ('Eval Pass Rate ' + Math.round(pct) + ' percent') : 'Eval Pass Rate Not Run Yet',
    });

    // Background track
    svg.appendChild(svgEl('circle', {
      cx: cx,
      cy: cy,
      r: r,
      fill: 'none',
      stroke: 'var(--chart-track)',
      'stroke-width': strokeW,
    }));

    // Progress arc (omitted when not run or pct <= 0 to avoid stray round-cap dot at 12 o'clock)
    if (hasValue && pct > 0) {
      const offset = circ * (1 - pct / 100);
      svg.appendChild(svgEl('circle', {
        cx: cx,
        cy: cy,
        r: r,
        fill: 'none',
        stroke: ringColor,
        'stroke-width': strokeW,
        'stroke-linecap': 'round',
        'stroke-dasharray': circ.toFixed(2),
        'stroke-dashoffset': offset.toFixed(2),
        transform: 'rotate(-90 ' + cx + ' ' + cy + ')',
      }));
    }

    // Center percentage
    svg.appendChild(svgEl('text', {
      x: cx,
      y: cy + 3,
      'text-anchor': 'middle',
      fill: 'var(--text-primary)',
      'font-size': '23',
      'font-weight': '700',
      'font-family': 'var(--font-sans)',
    }, hasValue ? (Math.round(pct) + '%') : '--'));

    // Subtitle below percentage
    const subText = hasCounts
      ? (passedNum + '/' + totalNum + ' PASS')
      : 'NOT RUN';
    svg.appendChild(svgEl('text', {
      x: cx,
      y: cy + 21,
      'text-anchor': 'middle',
      fill: 'var(--text-muted)',
      'font-size': '9.5',
      'font-weight': '700',
      'letter-spacing': '0.06em',
    }, subText));

    container.appendChild(svg);
  }

  global.PulseCharts = {
    renderMonthlyBudgetActualChart: renderMonthlyBudgetActualChart,
    renderDepartmentVarianceChart: renderDepartmentVarianceChart,
    renderEvalGauge: renderEvalGauge,
    hideTooltip: hideTooltip,
  };
})(window);
