// BDO Optima — app.js (v5: TraCI entegrasyonu)
document.addEventListener('DOMContentLoaded', () => {
    const runBtn       = document.getElementById('runPipelineBtn');
    const consoleLog   = document.getElementById('consoleLog');
    const mapDisplay   = document.getElementById('mapDisplay');
    const nodeCount    = document.getElementById('nodeCount');
    const mapLegend    = document.getElementById('mapLegend');
    const mapTooltip   = document.getElementById('mapTooltip');
    const leafletMapEl = document.getElementById('leafletMap');

    const dataSource      = document.getElementById('dataSource');
    const lsystemControls = document.getElementById('lsystemControls');
    const osmControls     = document.getElementById('osmControls');
    const osmPreset       = document.getElementById('osmPreset');
    const osmCustomGroup  = document.getElementById('osmCustomGroup');
    const osmCustom       = document.getElementById('osmCustom');
    const osmRadius       = document.getElementById('osmRadius');
    const tripsInput      = document.getElementById('tripsInput');
    const viewMode        = document.getElementById('viewMode');

    // YENİ: L-System preset + TraCI ayarları
    const lsystemPreset   = document.getElementById('lsystemPreset');
    const traciDuration   = document.getElementById('traciDuration');
    const runTraciCheck   = document.getElementById('runTraci');

    const statsPanel   = document.getElementById('statsPanel');
    const statsSource  = document.getElementById('statsSource');
    const statNodes    = document.getElementById('statNodes');
    const statEdges    = document.getElementById('statEdges');
    const statAvgDeg   = document.getElementById('statAvgDeg');
    const statAI       = document.getElementById('statAI');
    const sumoStatus   = document.getElementById('sumoStatus');

    const comparePanel  = document.getElementById('comparePanel');
    const cmpMaxBefore  = document.getElementById('cmpMaxBefore');
    const cmpMaxAfter   = document.getElementById('cmpMaxAfter');
    const cmpMaxDiff    = document.getElementById('cmpMaxDiff');
    const cmpAvgBefore  = document.getElementById('cmpAvgBefore');
    const cmpAvgAfter   = document.getElementById('cmpAvgAfter');
    const cmpAvgDiff    = document.getElementById('cmpAvgDiff');

    // YENİ: TraCI paneli
    const traciPanel         = document.getElementById('traciPanel');
    const traciMetrics       = document.getElementById('traciMetrics');
    const traciDurationLabel = document.getElementById('traciDurationLabel');
    const sumoLaunchBlock    = document.getElementById('sumoLaunchBlock');
    const sumoLaunchCmds     = document.getElementById('sumoLaunchCmds');

    let leafletMap = null;
    let buildingLayer = null;
    let currentResult = null;
    let currentViewMode = 'after';

    console.log("[BDO] app.js v5 (TraCI)");

    function highwayWidth(highway, isLeaflet) {
        const base = isLeaflet ? 1 : 0.7;
        const scale = {
            "motorway": 6.0, "motorway_link": 3.0,
            "trunk": 5.0,    "trunk_link": 2.5,
            "primary": 4.0,  "primary_link": 2.5,
            "secondary": 3.0,"secondary_link": 2.0,
            "tertiary": 2.5, "tertiary_link": 2.0,
            "unclassified": 2.0, "residential": 1.8, "": 2.0,
        };
        return (scale[highway] || 2.0) * base;
    }

    dataSource.addEventListener('change', () => {
        if (dataSource.value === 'osm') {
            lsystemControls.style.display = 'none';
            osmControls.style.display = '';
        } else {
            lsystemControls.style.display = '';
            osmControls.style.display = 'none';
        }
    });

    osmPreset.addEventListener('change', () => {
        osmCustomGroup.style.display = (osmPreset.value === 'custom') ? '' : 'none';
    });

    if (viewMode) {
        viewMode.addEventListener('change', (e) => {
            if (e.target.name === 'viewMode') {
                currentViewMode = e.target.value;
                if (currentResult) {
                    drawNetwork(currentResult.data, currentResult.source,
                                currentResult.buildings);
                }
            }
        });
    }

    function log(message, type = "normal") {
        const colors = { success: "#4ade80", warning: "#f87171",
                         info: "#60a5fa", normal: "#e2e8f0" };
        const color = colors[type] || colors.normal;
        const time = new Date().toLocaleTimeString('tr-TR', {
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
        consoleLog.innerHTML +=
            `<span style="color:rgba(255,255,255,0.25)">[${time}]</span> ` +
            `<span style="color:${color}">${message}</span>\n`;
        consoleLog.scrollTop = consoleLog.scrollHeight;
    }

    function parseToon(toonString) {
        const nodes = {}, links = [];
        if (!toonString) return { nodes, links };

        toonString.split('\n').forEach(line => {
            const parts = line.trim().split(';');
            if (parts[0] === 'NODE' && parts.length >= 4) {
                const x = parseFloat(parts[2]);
                const y = parseFloat(parts[3]);
                if (!Number.isFinite(x) || !Number.isFinite(y)) return;
                const node = { id: parts[1], x, y };
                if (parts.length >= 6) {
                    const la = parseFloat(parts[4]);
                    const lo = parseFloat(parts[5]);
                    if (Number.isFinite(la) && Number.isFinite(lo)) {
                        node.lat = la; node.lon = lo;
                    }
                }
                nodes[parts[1]] = node;
            } else if (parts[0] === 'EDGE' && parts.length >= 4) {
                links.push({
                    id: parts[1], source: parts[2], target: parts[3],
                    name: parts[4] || "",
                    highway: parts[5] || "",
                    maxspeed: parts[6] ? parseInt(parts[6]) || 40 : 40,
                    oneway: parts[7] === "1",
                });
            }
        });
        return { nodes, links };
    }

    function loadToColor(load, maxLoad) {
        if (load === 0 || maxLoad === 0) return "#cbd5e1";
        const t = Math.min(load / maxLoad, 1);
        if (t < 0.33) {
            const lt = t / 0.33;
            return `rgb(${Math.round(74+(234-74)*lt)},${Math.round(222+(179-222)*lt)},${Math.round(128+(8-128)*lt)})`;
        } else if (t < 0.66) {
            const lt = (t - 0.33) / 0.33;
            return `rgb(${Math.round(234+(249-234)*lt)},${Math.round(179+(115-179)*lt)},${Math.round(8+(22-8)*lt)})`;
        } else {
            const lt = (t - 0.66) / 0.34;
            return `rgb(${Math.round(249+(220-249)*lt)},${Math.round(115+(38-115)*lt)},${Math.round(22+(38-22)*lt)})`;
        }
    }

    function activeTrafficData() {
        if (!currentResult || !currentResult.simulation) return null;
        const t = currentResult.simulation;
        if (currentViewMode === 'before' || !t.after) return t.before;
        return t.after;
    }

    // ─────────────────────────────────────────────────────────────────────
    //  YENİ: TraCI panelini doldur
    // ─────────────────────────────────────────────────────────────────────
    function diffBadge(beforeVal, afterVal, lowerIsBetter = true) {
        if (beforeVal === null || beforeVal === undefined ||
            afterVal === null || afterVal === undefined ||
            beforeVal === 0) {
            return { text: '—', cls: 'is-neutral' };
        }
        const pct = ((afterVal - beforeVal) / beforeVal) * 100;
        const isImprovement = lowerIsBetter ? (pct < 0) : (pct > 0);
        const arrow = pct > 0 ? '↑' : (pct < 0 ? '↓' : '=');
        const cls = isImprovement ? 'is-good' : (pct === 0 ? 'is-neutral' : 'is-bad');
        return { text: `${arrow} ${Math.abs(pct).toFixed(1)}%`, cls };
    }

    function metricRow(label, before, after, unit, lowerIsBetter = true) {
        const beforeText = before !== null && before !== undefined ? `${before}${unit}` : '—';
        const afterText  = after  !== null && after  !== undefined ? `${after}${unit}`  : '—';
        const diff = diffBadge(before, after, lowerIsBetter);
        return `
            <div class="traci-metric-row">
                <div class="traci-metric-label">${label}</div>
                <div class="traci-metric-before">${beforeText}</div>
                <div class="traci-metric-arrow">→</div>
                <div class="traci-metric-after">${afterText}</div>
                <div class="traci-metric-diff ${diff.cls}">${diff.text}</div>
            </div>`;
    }

    function updateTraciPanel(result) {
        const sm = result.sumo_metrics;
        if (!sm || !sm.before || !sm.before.ok) {
            traciPanel.style.display = 'none';
            return;
        }

        const before = sm.before;
        const after  = sm.after && sm.after.ok ? sm.after : null;

        traciDurationLabel.textContent = `${before.total_steps}s · ${before.total_vehicles} araç`;

        // Ana metrikler
        let html = '';
        html += metricRow('Ort. Bekleme',  before.avg_wait_time,   after?.avg_wait_time,   's',     true);
        html += metricRow('Ort. Yolculuk', before.avg_travel_time, after?.avg_travel_time, 's',     true);
        html += metricRow('Ort. Hız',      before.avg_speed,       after?.avg_speed,       ' km/h', false);
        html += metricRow('Tamamlanan',    before.completed_vehicles, after?.completed_vehicles, '/' + before.total_vehicles, false);
        html += metricRow('CO₂',           before.total_co2 ? Math.round(before.total_co2/1000) : null,
                                          after?.total_co2 ? Math.round(after.total_co2/1000) : null,
                                          ' g', true);
        traciMetrics.innerHTML = html;

        // SUMO GUI komutları
        const folders = result.sumo_folders;
        if (folders && (folders.before || folders.after)) {
            let cmds = '';
            if (folders.before) {
                const cmd = `sumo-gui -c ${folders.before.replace(/\//g, '\\')}\\network.sumocfg`;
                cmds += `
                    <div class="sumo-launch-cmd" onclick="copyToClipboard(this, '${cmd}')">
                        <span><span style="opacity:.5">[ÖNCE]</span> ${cmd}</span>
                        <span class="cmd-copy-icon">📋 kopyala</span>
                    </div>`;
            }
            if (folders.after) {
                const cmd = `sumo-gui -c ${folders.after.replace(/\//g, '\\')}\\network.sumocfg`;
                cmds += `
                    <div class="sumo-launch-cmd" onclick="copyToClipboard(this, '${cmd}')">
                        <span><span style="opacity:.5">[SONRA]</span> ${cmd}</span>
                        <span class="cmd-copy-icon">📋 kopyala</span>
                    </div>`;
            }
            sumoLaunchCmds.innerHTML = cmds;
            sumoLaunchBlock.style.display = '';
        } else {
            sumoLaunchBlock.style.display = 'none';
        }

        traciPanel.style.display = '';
    }

    // Global yardımcı: clipboard kopyalama
    window.copyToClipboard = function(el, text) {
        navigator.clipboard.writeText(text).then(() => {
            const orig = el.querySelector('.cmd-copy-icon').textContent;
            el.querySelector('.cmd-copy-icon').textContent = '✓ kopyalandı';
            el.classList.add('copied');
            setTimeout(() => {
                el.querySelector('.cmd-copy-icon').textContent = orig;
                el.classList.remove('copied');
            }, 1500);
        }).catch(err => console.error('Clipboard:', err));
    };

    function updateStats(result) {
        const s = result.stats || {};
        statsSource.textContent = result.source || '—';
        statNodes.textContent = s.nodes ?? '—';
        statEdges.textContent = s.edges ?? '—';
        statAvgDeg.textContent = s.avg_degree ?? '—';

        if (s.ai_bypass_added) {
            statAI.textContent = '✓';
            statAI.className = 'stat-cell-value is-yes';
        } else {
            statAI.textContent = 'Pas';
            statAI.className = 'stat-cell-value is-no';
        }
        statsPanel.style.display = '';

        sumoStatus.className = 'sumo-status';
        const sumo = result.sumo;
        if (sumo && sumo.net_xml) {
            sumoStatus.className += ' is-ok';
            sumoStatus.innerHTML = `✓ SUMO: .net.xml hazır`;
        } else if (sumo && sumo.nod_xml) {
            sumoStatus.className += ' is-partial';
            sumoStatus.innerHTML = `⚠ SUMO XML'leri yazıldı`;
        }

        // Python sim karşılaştırma
        const cmp = result.simulation?.comparison;
        if (cmp) {
            cmpMaxBefore.textContent = cmp.max_load_before;
            cmpMaxAfter.textContent  = cmp.max_load_after;
            cmpAvgBefore.textContent = cmp.avg_load_before;
            cmpAvgAfter.textContent  = cmp.avg_load_after;

            const mi = cmp.max_load_improvement_pct;
            const ai = cmp.avg_load_improvement_pct;
            cmpMaxDiff.textContent = `${mi>0?'↓':(mi<0?'↑':'=')} ${Math.abs(mi)}%`;
            cmpMaxDiff.className = 'compare-diff ' + (mi>0?'is-good':mi<0?'is-bad':'is-neutral');
            cmpAvgDiff.textContent = `${ai>0?'↓':(ai<0?'↑':'=')} ${Math.abs(ai)}%`;
            cmpAvgDiff.className = 'compare-diff ' + (ai>0?'is-good':ai<0?'is-bad':'is-neutral');
            comparePanel.style.display = '';
        } else {
            comparePanel.style.display = 'none';
        }

        // YENİ: TraCI paneli
        updateTraciPanel(result);
    }

    function attachNodeHover(selection, degree) {
        selection
            .on("mouseover", function (event, d) {
                const baseR = parseFloat(d3.select(this).attr("r"));
                d3.select(this).attr("fill", "#ef4444").attr("r", baseR + 4);
                const deg = degree[d.id] || 0;
                const label = deg >= 3 ? 'Kavşak' : deg === 1 ? 'Çıkmaz' : 'Bağlantı';
                const coord = (d.lat !== undefined)
                    ? `(${d.lat.toFixed(5)}, ${d.lon.toFixed(5)})`
                    : `(${d.x.toFixed(1)}, ${d.y.toFixed(1)})`;
                mapTooltip.innerHTML = `
                    <div><strong>${d.id}</strong> — ${label}</div>
                    <div class="map-tooltip-coord">${coord} · derece: ${deg}</div>`;
                mapTooltip.style.display = 'block';
            })
            .on("mousemove", function (event) {
                const pr = mapDisplay.getBoundingClientRect();
                mapTooltip.style.left = (event.clientX - pr.left) + 'px';
                mapTooltip.style.top  = (event.clientY - pr.top) + 'px';
            })
            .on("mouseout", function (event, d) {
                d3.select(this).attr("fill", "#c8392b")
                    .attr("r", 3.5 + Math.min((degree[d.id] || 1) * 0.4, 3));
                mapTooltip.style.display = 'none';
            });
    }

    function attachEdgeHover(selection, trafficData) {
        selection
            .on("mouseover", function (event, d) {
                const load = trafficData?.edge_loads?.[d.id] || 0;
                const maxLoad = trafficData?.max_load || 1;
                const pct = Math.round((load / maxLoad) * 100);
                const isAI = d.id && d.id.startsWith("LLM_");

                const cur = parseFloat(d3.select(this).attr("stroke-width"));
                d3.select(this).attr("stroke-opacity", 1).attr("stroke-width", cur + 1.5);

                let label = isAI ? '🤖 AI Bypass' : (d.name || d.id);
                let html = `<div><strong>${label}</strong></div>`;
                const tags = [];
                if (d.highway) tags.push(d.highway);
                if (d.maxspeed) tags.push(`${d.maxspeed} km/h`);
                if (d.oneway) tags.push('tek yön');
                if (tags.length) html += `<div class="map-tooltip-coord">${tags.join(' · ')}</div>`;
                if (trafficData) {
                    html += `<div class="map-tooltip-coord" style="margin-top:3px;">
                        <strong>${load}</strong> araç (max'ın %${pct}'i)</div>`;
                }
                mapTooltip.innerHTML = html;
                mapTooltip.style.display = 'block';
            })
            .on("mousemove", function (event) {
                const pr = mapDisplay.getBoundingClientRect();
                mapTooltip.style.left = (event.clientX - pr.left) + 'px';
                mapTooltip.style.top  = (event.clientY - pr.top) + 'px';
            })
            .on("mouseout", function (event, d) {
                const isAI = d.id && d.id.startsWith("LLM_");
                d3.select(this)
                    .attr("stroke-opacity", isAI ? 0.95 : 0.85)
                    .attr("stroke-width", baseStrokeWidth(d, false));
                mapTooltip.style.display = 'none';
            });
    }

    function baseStrokeWidth(d, isLeaflet) {
        if (d.id && d.id.startsWith("LLM_")) return isLeaflet ? 4.5 : 3.5;
        if (d.highway) return highwayWidth(d.highway, isLeaflet);
        return isLeaflet ? 3 : 2.5;
    }

    function ensureArrowMarker(svg) {
        let defs = svg.select("defs");
        if (defs.empty()) defs = svg.append("defs");
        if (defs.select("#arrowhead").empty()) {
            const m = defs.append("marker")
                .attr("id", "arrowhead").attr("viewBox", "0 0 10 10")
                .attr("refX", "8").attr("refY", "5")
                .attr("markerWidth", 6).attr("markerHeight", 6)
                .attr("orient", "auto-start-reverse");
            m.append("path").attr("d", "M 0 0 L 10 5 L 0 10 z")
                .attr("fill", "rgba(60,60,60,0.7)");
        }
    }

    function drawSynthetic(nodes, links) {
        leafletMapEl.style.display = 'none';
        d3.select(mapDisplay).selectAll("svg").remove();

        const rect = mapDisplay.getBoundingClientRect();
        const width = rect.width || 800, height = rect.height || 460;

        const svg = d3.select(mapDisplay).append("svg")
            .attr("width", width).attr("height", height)
            .attr("viewBox", `0 0 ${width} ${height}`)
            .attr("preserveAspectRatio", "xMidYMid meet")
            .style("display", "block").style("cursor", "grab")
            .style("position", "relative").style("z-index", 2);

        ensureArrowMarker(svg);

        const defs = svg.select("defs");
        defs.append("pattern").attr("id", "grid")
            .attr("width", 40).attr("height", 40)
            .attr("patternUnits", "userSpaceOnUse")
            .append("path").attr("d", "M 40 0 L 0 0 0 40")
            .attr("fill", "none").attr("stroke", "rgba(10,10,15,0.06)")
            .attr("stroke-width", 1);

        svg.append("rect").attr("width", width).attr("height", height)
            .attr("fill", "url(#grid)");

        const g = svg.append("g");
        svg.call(d3.zoom().scaleExtent([0.05, 15])
            .on("zoom", e => g.attr("transform", e.transform)));

        const xExtent = d3.extent(nodes, d => d.x);
        const yExtent = d3.extent(nodes, d => d.y);
        const padIfZero = ([a, b]) => a === b ? [a-1, b+1] : [a, b];
        const xScale = d3.scaleLinear().domain(padIfZero(xExtent)).range([60, width-60]);
        const yScale = d3.scaleLinear().domain(padIfZero(yExtent)).range([height-60, 60]);

        const degree = {};
        links.forEach(l => {
            degree[l.source.id] = (degree[l.source.id]||0) + 1;
            degree[l.target.id] = (degree[l.target.id]||0) + 1;
        });

        const traffic = activeTrafficData();
        const edgeLoads = traffic?.edge_loads || {};
        const maxLoad = traffic?.max_load || 1;

        const edgeSel = g.append("g").selectAll("line")
            .data(links).join("line")
            .attr("stroke", d => d.id && d.id.startsWith("LLM_")
                ? "#16a34a"
                : loadToColor(edgeLoads[d.id] || 0, maxLoad))
            .attr("stroke-width", d => baseStrokeWidth(d, false))
            .attr("stroke-opacity", d => d.id && d.id.startsWith("LLM_") ? 0.95 : 0.85)
            .attr("stroke-dasharray", d => d.id && d.id.startsWith("LLM_") ? "6 4" : null)
            .attr("stroke-linecap", "round")
            .attr("marker-end", d => d.oneway ? "url(#arrowhead)" : null)
            .attr("x1", d => xScale(d.source.x)).attr("y1", d => yScale(d.source.y))
            .attr("x2", d => xScale(d.target.x)).attr("y2", d => yScale(d.target.y))
            .style("cursor", "help");
        attachEdgeHover(edgeSel, traffic);

        const nodeSel = g.append("g").selectAll("circle")
            .data(nodes).join("circle")
            .attr("cx", d => xScale(d.x)).attr("cy", d => yScale(d.y))
            .attr("r", d => 3.5 + Math.min((degree[d.id]||1) * 0.4, 3))
            .attr("fill", "#c8392b").attr("stroke", "#f5f3ee")
            .attr("stroke-width", 1.5).style("cursor", "pointer");
        attachNodeHover(nodeSel, degree);

        mapLegend.style.display = '';
    }

    function drawOSM(nodes, links, buildings) {
        const geoNodes = nodes.filter(n => n.lat !== undefined);
        if (geoNodes.length === 0) {
            log("[ERR] OSM verisinde coğrafi koordinat yok", "warning");
            return;
        }

        d3.select(mapDisplay).selectAll("svg").remove();
        leafletMapEl.style.display = '';

        if (leafletMap) {
            leafletMap.remove();
            leafletMap = null;
            buildingLayer = null;
        }

        const avgLat = d3.mean(geoNodes, d => d.lat);
        const avgLon = d3.mean(geoNodes, d => d.lon);

        leafletMap = L.map(leafletMapEl).setView([avgLat, avgLon], 16);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors',
        }).addTo(leafletMap);

        if (buildings && buildings.geo && buildings.geo.length > 0) {
            buildingLayer = L.layerGroup();
            buildings.geo.forEach(coords => {
                if (coords.length < 3) return;
                const latLngs = coords.map(([la, lo]) => [la, lo]);
                L.polygon(latLngs, {
                    color: '#7c7c7c',
                    weight: 0.6,
                    fillColor: '#a8a8a8',
                    fillOpacity: 0.35,
                    interactive: false,
                }).addTo(buildingLayer);
            });
            buildingLayer.addTo(leafletMap);
            log(`[MAP] ${buildings.geo.length} bina haritada çizildi`, "info");
        }

        L.svg({ clickable: true }).addTo(leafletMap);
        const overlay = d3.select(leafletMap.getPanes().overlayPane).select('svg')
            .attr("pointer-events", "auto");
        const g = overlay.select('g').attr("class", "leaflet-zoom-hide");

        ensureArrowMarker(overlay);

        const degree = {};
        links.forEach(l => {
            degree[l.source.id] = (degree[l.source.id]||0) + 1;
            degree[l.target.id] = (degree[l.target.id]||0) + 1;
        });

        const geoNodeIds = new Set(geoNodes.map(n => n.id));
        const geoLinks = links.filter(l =>
            geoNodeIds.has(l.source.id) && geoNodeIds.has(l.target.id)
        );

        const traffic = activeTrafficData();
        const edgeLoads = traffic?.edge_loads || {};
        const maxLoad = traffic?.max_load || 1;

        const edgeSel = g.selectAll("line").data(geoLinks).join("line")
            .attr("stroke", d => d.id && d.id.startsWith("LLM_")
                ? "#16a34a"
                : loadToColor(edgeLoads[d.id] || 0, maxLoad))
            .attr("stroke-width", d => baseStrokeWidth(d, true))
            .attr("stroke-opacity", d => d.id && d.id.startsWith("LLM_") ? 0.95 : 0.9)
            .attr("stroke-dasharray", d => d.id && d.id.startsWith("LLM_") ? "8 5" : null)
            .attr("stroke-linecap", "round")
            .attr("marker-end", d => d.oneway ? "url(#arrowhead)" : null)
            .style("cursor", "help");
        attachEdgeHover(edgeSel, traffic);

        const nodeSel = g.selectAll("circle").data(geoNodes).join("circle")
            .attr("r", d => 4 + Math.min((degree[d.id]||1) * 0.4, 3))
            .attr("fill", "#c8392b").attr("stroke", "#ffffff")
            .attr("stroke-width", 2).style("cursor", "pointer");
        attachNodeHover(nodeSel, degree);

        function project(node) {
            return leafletMap.latLngToLayerPoint([node.lat, node.lon]);
        }
        function redraw() {
            edgeSel
                .attr("x1", d => project(d.source).x).attr("y1", d => project(d.source).y)
                .attr("x2", d => project(d.target).x).attr("y2", d => project(d.target).y);
            nodeSel.attr("cx", d => project(d).x).attr("cy", d => project(d).y);
        }
        leafletMap.on("zoomend viewreset moveend", redraw);
        redraw();

        const bounds = L.latLngBounds(geoNodes.map(n => [n.lat, n.lon]));
        leafletMap.fitBounds(bounds, { padding: [30, 30] });
        mapLegend.style.display = '';
    }

    function drawNetwork(toonString, source, buildings) {
        mapDisplay.querySelectorAll('.map-empty-state').forEach(el => el.remove());
        mapDisplay.appendChild(mapLegend);
        mapDisplay.appendChild(mapTooltip);
        mapDisplay.appendChild(leafletMapEl);

        const { nodes, links } = parseToon(toonString);
        const nodesArray = Object.values(nodes);
        const linksArray = links
            .map(l => ({ ...l, source: nodes[l.source], target: nodes[l.target] }))
            .filter(l => l.source && l.target);

        if (nodesArray.length === 0) {
            const err = document.createElement('div');
            err.className = 'map-empty-state';
            err.style.color = '#f87171';
            err.innerHTML = `<p>// ERR: Veri ayrıştırılamadı</p>`;
            mapDisplay.appendChild(err);
            return;
        }

        if (nodeCount) {
            nodeCount.textContent = `${nodesArray.length} kavşak · ${linksArray.length} kenar`;
        }

        const hasGeo = nodesArray.some(n => n.lat !== undefined);
        if (hasGeo && source === 'OSM') {
            drawOSM(nodesArray, linksArray, buildings);
        } else {
            drawSynthetic(nodesArray, linksArray);
        }
    }

    function buildRequestUrl() {
        const trips = (tripsInput?.value) || 300;
        const traciDur = (traciDuration?.value) || 150;
        const runTraciFlag = runTraciCheck?.checked ? 'true' : 'false';
        const traciParams = `&run_traci=${runTraciFlag}&traci_duration=${traciDur}`;

        if (dataSource.value === 'osm') {
            let lat, lon;
            if (osmPreset.value === 'custom') {
                const raw = (osmCustom.value || '').split(',').map(s => parseFloat(s.trim()));
                if (raw.length !== 2 || !isFinite(raw[0]) || !isFinite(raw[1])) {
                    throw new Error("Koordinat formatı: enlem, boylam");
                }
                [lat, lon] = raw;
            } else {
                [lat, lon] = osmPreset.value.split(',').map(parseFloat);
            }
            const radius = osmRadius.value || 500;
            return {
                url: `http://127.0.0.1:8000/generate_osm?lat=${lat}&lon=${lon}&radius=${radius}&num_trips=${trips}${traciParams}`,
                label: `OSM (${lat.toFixed(4)}, ${lon.toFixed(4)}, r=${radius}m, ${trips} sürücü)`,
            };
        } else {
            const iter = document.getElementById('iterations').value;
            const snap = document.getElementById('snapping').value;
            const preset = lsystemPreset?.value || 'default';
            return {
                url: `http://127.0.0.1:8000/generate?iterations=${iter}&snapping=${snap}&num_trips=${trips}&preset=${preset}${traciParams}`,
                label: `L-System (${preset}, iter=${iter}, ${trips} sürücü)`,
            };
        }
    }

    if (runBtn) {
        runBtn.addEventListener('click', async () => {
            let requestInfo;
            try {
                requestInfo = buildRequestUrl();
            } catch (e) {
                log(`[ERR] ${e.message}`, "warning");
                return;
            }

            runBtn.disabled = true;
            runBtn.innerHTML = '<span style="animation: spin 1s linear infinite; display:inline-block">⟳</span> Çalışıyor...';
            consoleLog.innerHTML = "";
            if (nodeCount) nodeCount.textContent = "—";
            leafletMapEl.style.display = 'none';
            d3.select(mapDisplay).selectAll("svg").remove();

            // Eski panelleri sıfırla
            traciPanel.style.display = 'none';
            comparePanel.style.display = 'none';

            mapDisplay.innerHTML = '';
            mapDisplay.appendChild(mapLegend);
            mapDisplay.appendChild(mapTooltip);
            mapDisplay.appendChild(leafletMapEl);
            const loader = document.createElement('div');
            loader.className = 'map-empty-state';
            const traciOn = runTraciCheck?.checked;
            const dur = traciDuration?.value || 150;
            loader.innerHTML = `
                <div class="empty-icon" style="animation: spin 1s linear infinite;">⟳</div>
                <p>// Pipeline çalışıyor...</p>
                <p style="font-size:0.7rem;opacity:0.5;margin-top:0.4rem;">
                    L-Sistem → Python sim → Gemini → ${traciOn ? `TraCI (~${dur*2}s)` : 'sadece XML'}
                </p>`;
            mapDisplay.appendChild(loader);

            log(`[REQ] ${requestInfo.label}`, "info");
            if (traciOn) {
                log(`[TraCI] Aktif (${dur}s × 2 ölçüm). Lütfen bekleyin...`, "info");
            }

            try {
                const response = await fetch(requestInfo.url);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const result = await response.json();

                if (result.status === "error") {
                    throw new Error(result.message || "Sunucu hatası");
                }
                if (!result.data) throw new Error("Backend boş döndü");

                currentResult = result;
                currentViewMode = result.simulation?.after ? 'after' : 'before';

                const radios = document.querySelectorAll('input[name="viewMode"]');
                radios.forEach(r => {
                    r.checked = (r.value === currentViewMode);
                    r.disabled = (r.value === 'after' && !result.simulation?.after);
                });

                const srcLabel = result.source === 'OSM'
                    ? `OSM'den ${result.stats?.nodes || '?'} kavşak alındı`
                    : "C++ motoru ağ üretti";
                log(`[OK] ${srcLabel}`, "success");

                if (result.buildings && result.buildings.count > 0) {
                    log(`[OSM] ${result.buildings.count} bina çekildi`, "info");
                }

                const before = result.simulation?.before;
                if (before) {
                    log(`[SIM] Önce: max ${before.max_load}, ort ${before.avg_load}`, "info");
                }

                if (result.stats?.ai_bypass_added) {
                    const cmp = result.simulation?.comparison;
                    if (cmp) {
                        const imp = cmp.max_load_improvement_pct;
                        if (imp > 0) {
                            log(`[SIM] Sonra: max yük %${imp} azaldı → ${cmp.max_load_after}`, "success");
                        } else if (imp < 0) {
                            log(`[SIM] Sonra: yük %${-imp} arttı`, "warning");
                        } else {
                            log(`[SIM] Değişim yok`, "warning");
                        }
                    }
                } else {
                    log("[AI] Bypass eklenmedi (geçerli aday yok ya da hata)", "warning");
                }

                // YENİ: TraCI sonuçlarını logla
                if (result.sumo_metrics?.before?.ok) {
                    const tb = result.sumo_metrics.before;
                    log(`[TraCI] Önce: bekleme ${tb.avg_wait_time}s, hız ${tb.avg_speed} km/h, ` +
                        `tamamlanan ${tb.completed_vehicles}/${tb.total_vehicles}`, "info");
                }
                if (result.sumo_metrics?.after?.ok) {
                    const ta = result.sumo_metrics.after;
                    log(`[TraCI] Sonra: bekleme ${ta.avg_wait_time}s, hız ${ta.avg_speed} km/h, ` +
                        `tamamlanan ${ta.completed_vehicles}/${ta.total_vehicles}`, "info");
                }
                if (result.sumo_comparison) {
                    const sc = result.sumo_comparison;
                    log(`[TraCI] ✓ Bekleme süresi iyileşmesi: %${sc.avg_wait_improvement_pct}`,
                        sc.avg_wait_improvement_pct > 0 ? "success" : "warning");
                    log(`[TraCI] ✓ Ortalama hız iyileşmesi: %${sc.avg_speed_improvement_pct}`,
                        sc.avg_speed_improvement_pct > 0 ? "success" : "warning");
                }

                if (result.sumo?.net_xml) {
                    log("[OK] SUMO → .net.xml ✓", "success");
                } else if (result.sumo?.nod_xml) {
                    log("[OK] SUMO → XML'ler yazıldı", "warning");
                }

                drawNetwork(result.data, result.source, result.buildings);
                updateStats(result);
                log("[DONE] Pipeline tamamlandı ✓", "success");

            } catch (error) {
                console.error("[BDO] Hata:", error);
                log(`[ERR] ${error.message}`, "warning");
                mapDisplay.innerHTML = '';
                mapDisplay.appendChild(mapLegend);
                mapDisplay.appendChild(mapTooltip);
                mapDisplay.appendChild(leafletMapEl);
                const err = document.createElement('div');
                err.className = 'map-empty-state';
                err.style.color = '#f87171';
                err.innerHTML = `
                    <div class="empty-icon">⚠</div>
                    <p>// Pipeline başarısız</p>
                    <p style="font-size:0.72rem; opacity:0.6;">${error.message}</p>`;
                mapDisplay.appendChild(err);
            } finally {
                runBtn.disabled = false;
                runBtn.innerHTML = '<span>▶</span> Pipeline\'ı Çalıştır';
            }
        });
    }
});