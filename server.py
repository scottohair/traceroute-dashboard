#!/usr/bin/env python3
"""Traceroute Dashboard - runs traceroutes, geolocates hops, serves interactive map."""

import json
import subprocess
import re
import os
import sys
import time
import threading
import http.server
import socketserver
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).parent
TARGETS_FILE = BASE_DIR / "targets.json"
RESULTS_FILE = BASE_DIR / "results.json"
HTML_FILE = BASE_DIR / "index.html"
PORT = 12034

GEO_CACHE = {}
GEO_LOCK = threading.Lock()


def run_traceroute(host, max_hops=20):
    """Run traceroute and parse output into structured hops."""
    try:
        result = subprocess.run(
            ["traceroute", "-m", str(max_hops), "-q", "1", "-w", "2", host],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            result = subprocess.run(
                ["traceroute", "-m", str(max_hops), "-q", "1", host],
                capture_output=True, text=True, timeout=60
            )
            output = result.stdout
        except Exception as e:
            return {"error": str(e), "hops": []}

    hops = []
    for line in output.strip().split("\n")[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        # Match: hop_num  hostname (ip)  time ms  OR  hop_num  * * *
        m = re.match(r'\s*(\d+)\s+(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)\s+([\d.]+)\s*ms', line)
        if m:
            hops.append({
                "hop": int(m.group(1)),
                "host": m.group(2),
                "ip": m.group(3),
                "rtt_ms": float(m.group(4))
            })
        else:
            # Try: hop_num  ip  time ms
            m2 = re.match(r'\s*(\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+([\d.]+)\s*ms', line)
            if m2:
                hops.append({
                    "hop": int(m2.group(1)),
                    "host": m2.group(2),
                    "ip": m2.group(2),
                    "rtt_ms": float(m2.group(3))
                })
            else:
                # Timeout hop
                m3 = re.match(r'\s*(\d+)\s+\*', line)
                if m3:
                    hops.append({
                        "hop": int(m3.group(1)),
                        "host": "*",
                        "ip": None,
                        "rtt_ms": None
                    })
    return {"hops": hops, "raw": output}


def geolocate_ip(ip):
    """Get geolocation for an IP using ip-api.com (free, no key needed)."""
    if not ip or ip.startswith("10.") or ip.startswith("192.168.") or ip.startswith("172."):
        return None
    with GEO_LOCK:
        if ip in GEO_CACHE:
            return GEO_CACHE[ip]
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon,isp,org,as"
        req = urllib.request.Request(url, headers={"User-Agent": "TracerouteDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") == "success":
            geo = {
                "lat": data["lat"],
                "lon": data["lon"],
                "city": data.get("city", ""),
                "region": data.get("regionName", ""),
                "country": data.get("country", ""),
                "isp": data.get("isp", ""),
                "org": data.get("org", ""),
                "as": data.get("as", "")
            }
            with GEO_LOCK:
                GEO_CACHE[ip] = geo
            return geo
    except Exception:
        pass
    return None


def process_target(category, target):
    """Run traceroute and geolocate all hops for a target."""
    name = target["name"]
    host = target["host"]
    print(f"  [{category}] Tracing {name} ({host})...")
    tr = run_traceroute(host)
    # Collect unique IPs to geolocate
    ips = [h["ip"] for h in tr["hops"] if h["ip"]]
    unique_ips = list(dict.fromkeys(ips))  # preserve order, dedupe

    # Geolocate with rate limiting (ip-api allows 45/min for free)
    for ip in unique_ips:
        with GEO_LOCK:
            if ip in GEO_CACHE:
                continue
        geo = geolocate_ip(ip)
        time.sleep(0.15)  # rate limit

    # Attach geo to hops
    for hop in tr["hops"]:
        if hop["ip"]:
            with GEO_LOCK:
                hop["geo"] = GEO_CACHE.get(hop["ip"])
        else:
            hop["geo"] = None

    return {
        "name": name,
        "host": host,
        "category": category,
        "hop_count": len(tr["hops"]),
        "total_rtt": tr["hops"][-1]["rtt_ms"] if tr["hops"] and tr["hops"][-1]["rtt_ms"] else None,
        "hops": tr["hops"]
    }


def run_all_traceroutes():
    """Run traceroutes for all targets in parallel."""
    with open(TARGETS_FILE) as f:
        targets = json.load(f)

    results = []
    tasks = []
    for category, target_list in targets.items():
        for target in target_list:
            tasks.append((category, target))

    # Use 6 threads to stay under ip-api rate limit
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process_target, cat, tgt): (cat, tgt) for cat, tgt in tasks}
        for future in as_completed(futures):
            cat, tgt = futures[future]
            try:
                result = future.result()
                results.append(result)
                hops_with_geo = sum(1 for h in result["hops"] if h.get("geo"))
                print(f"  Done: {result['name']} - {result['hop_count']} hops, {hops_with_geo} geolocated")
            except Exception as e:
                print(f"  FAILED: {tgt['name']} - {e}")
                results.append({
                    "name": tgt["name"],
                    "host": tgt["host"],
                    "category": cat,
                    "hop_count": 0,
                    "total_rtt": None,
                    "hops": [],
                    "error": str(e)
                })

    # Sort by category then name
    cat_order = {"Quant APIs": 0, "Cloud Providers": 1, "NYSE & Financial": 2}
    results.sort(key=lambda r: (cat_order.get(r["category"], 9), r["name"]))

    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {RESULTS_FILE}")
    return results


def generate_html(results):
    """Generate the dashboard HTML."""
    html = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Traceroute Network Dashboard</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    background: #0a0e17;
    color: #c8d6e5;
    overflow-x: hidden;
  }
  .header {
    background: linear-gradient(135deg, #0d1321 0%, #1a1a2e 100%);
    border-bottom: 1px solid #1e3a5f;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 18px;
    color: #00d4ff;
    font-weight: 600;
    letter-spacing: 1px;
  }
  .header .stats {
    display: flex;
    gap: 24px;
    font-size: 12px;
  }
  .header .stat-item {
    text-align: center;
  }
  .header .stat-value {
    font-size: 20px;
    font-weight: 700;
    color: #00ff88;
  }
  .header .stat-label {
    color: #5a6c7d;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-size: 10px;
  }
  .main {
    display: grid;
    grid-template-columns: 340px 1fr;
    grid-template-rows: 55vh 1fr;
    height: calc(100vh - 60px);
  }
  .sidebar {
    grid-row: 1 / 3;
    background: #0d1321;
    border-right: 1px solid #1e3a5f;
    overflow-y: auto;
    padding: 0;
  }
  .sidebar::-webkit-scrollbar { width: 6px; }
  .sidebar::-webkit-scrollbar-track { background: #0d1321; }
  .sidebar::-webkit-scrollbar-thumb { background: #1e3a5f; border-radius: 3px; }
  .cat-header {
    padding: 10px 16px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 2px;
    position: sticky;
    top: 0;
    z-index: 10;
    border-bottom: 1px solid #1e3a5f;
  }
  .cat-quant { background: #1a0a2e; color: #a855f7; }
  .cat-cloud { background: #0a1e2e; color: #3b82f6; }
  .cat-finance { background: #1e1a0a; color: #f59e0b; }
  .target-item {
    padding: 10px 16px;
    border-bottom: 1px solid #111827;
    cursor: pointer;
    transition: background 0.2s;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .target-item:hover { background: #111827; }
  .target-item.active { background: #1e293b; border-left: 3px solid #00d4ff; }
  .target-name { font-size: 13px; font-weight: 500; }
  .target-host { font-size: 10px; color: #5a6c7d; margin-top: 2px; }
  .target-meta { text-align: right; }
  .target-rtt {
    font-size: 14px;
    font-weight: 700;
  }
  .rtt-good { color: #00ff88; }
  .rtt-mid { color: #f59e0b; }
  .rtt-bad { color: #ef4444; }
  .target-hops { font-size: 10px; color: #5a6c7d; }
  #map {
    background: #0a0e17;
    border-bottom: 1px solid #1e3a5f;
  }
  .detail-panel {
    background: #0d1321;
    overflow-y: auto;
    padding: 16px;
  }
  .detail-panel::-webkit-scrollbar { width: 6px; }
  .detail-panel::-webkit-scrollbar-track { background: #0d1321; }
  .detail-panel::-webkit-scrollbar-thumb { background: #1e3a5f; border-radius: 3px; }
  .hop-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .hop-table th {
    text-align: left;
    padding: 6px 10px;
    background: #111827;
    color: #5a6c7d;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-size: 10px;
    font-weight: 600;
    position: sticky;
    top: 0;
  }
  .hop-table td {
    padding: 5px 10px;
    border-bottom: 1px solid #111827;
  }
  .hop-table tr:hover td { background: #111827; }
  .hop-num { color: #5a6c7d; font-weight: 600; }
  .hop-ip { color: #00d4ff; }
  .hop-host { color: #c8d6e5; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hop-loc { color: #818cf8; }
  .hop-timeout { color: #374151; font-style: italic; }
  .rtt-bar {
    height: 6px;
    border-radius: 3px;
    min-width: 4px;
    display: inline-block;
    vertical-align: middle;
    margin-right: 6px;
  }
  .filter-bar {
    display: flex;
    gap: 8px;
    padding: 10px 16px;
    background: #0d1321;
    border-bottom: 1px solid #1e3a5f;
  }
  .filter-btn {
    padding: 4px 12px;
    border: 1px solid #1e3a5f;
    border-radius: 4px;
    background: transparent;
    color: #5a6c7d;
    font-family: inherit;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.2s;
  }
  .filter-btn:hover { border-color: #00d4ff; color: #00d4ff; }
  .filter-btn.active { background: #00d4ff22; border-color: #00d4ff; color: #00d4ff; }
  .detail-title {
    font-size: 14px;
    margin-bottom: 12px;
    color: #00d4ff;
  }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
    padding: 16px;
  }
  .summary-card {
    background: #111827;
    border: 1px solid #1e3a5f;
    border-radius: 8px;
    padding: 12px 16px;
  }
  .summary-card h3 { font-size: 12px; color: #5a6c7d; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  .leaflet-popup-content-wrapper {
    background: #1a1a2e !important;
    color: #c8d6e5 !important;
    border: 1px solid #1e3a5f !important;
    border-radius: 6px !important;
    font-family: 'SF Mono', monospace !important;
    font-size: 11px !important;
  }
  .leaflet-popup-tip { background: #1a1a2e !important; }
  .no-select { font-size: 13px; color: #374151; padding: 40px; text-align: center; }
</style>
</head>
<body>

<div class="header">
  <h1>TRACEROUTE NETWORK DASHBOARD</h1>
  <div class="stats">
    <div class="stat-item">
      <div class="stat-value" id="total-targets">0</div>
      <div class="stat-label">Targets</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" id="total-hops">0</div>
      <div class="stat-label">Total Hops</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" id="avg-rtt">-</div>
      <div class="stat-label">Avg RTT (ms)</div>
    </div>
    <div class="stat-item">
      <div class="stat-value" id="unique-ips">0</div>
      <div class="stat-label">Unique IPs</div>
    </div>
  </div>
</div>

<div class="main">
  <div class="sidebar">
    <div class="filter-bar">
      <button class="filter-btn active" data-filter="all">All</button>
      <button class="filter-btn" data-filter="Quant APIs">Quant</button>
      <button class="filter-btn" data-filter="Cloud Providers">Cloud</button>
      <button class="filter-btn" data-filter="NYSE & Financial">Finance</button>
    </div>
    <div id="target-list"></div>
  </div>
  <div id="map"></div>
  <div class="detail-panel" id="detail-panel">
    <div class="no-select">Select a target from the sidebar to view route details</div>
  </div>
</div>

<script>
const DATA = ''' + json.dumps(results) + ''';

const catColors = {
  "Quant APIs": {line: "#a855f7", css: "cat-quant"},
  "Cloud Providers": {line: "#3b82f6", css: "cat-cloud"},
  "NYSE & Financial": {line: "#f59e0b", css: "cat-finance"}
};

// Setup map
const map = L.map("map", {
  center: [38, -40],
  zoom: 3,
  zoomControl: false,
  attributionControl: false
});
L.control.zoom({position: "topright"}).addTo(map);

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 19
}).addTo(map);

let routeLayers = L.layerGroup().addTo(map);
let allHopMarkers = L.layerGroup().addTo(map);

// Compute stats
const totalTargets = DATA.length;
const totalHops = DATA.reduce((s, t) => s + t.hops.length, 0);
const rtts = DATA.filter(t => t.total_rtt).map(t => t.total_rtt);
const avgRtt = rtts.length ? (rtts.reduce((a, b) => a + b, 0) / rtts.length).toFixed(1) : "-";
const allIPs = new Set();
DATA.forEach(t => t.hops.forEach(h => { if (h.ip) allIPs.add(h.ip); }));

document.getElementById("total-targets").textContent = totalTargets;
document.getElementById("total-hops").textContent = totalHops;
document.getElementById("avg-rtt").textContent = avgRtt;
document.getElementById("unique-ips").textContent = allIPs.size;

// Build sidebar
function buildSidebar(filter) {
  const list = document.getElementById("target-list");
  list.innerHTML = "";
  let lastCat = "";
  const filtered = filter === "all" ? DATA : DATA.filter(d => d.category === filter);
  filtered.forEach((t, idx) => {
    if (t.category !== lastCat) {
      lastCat = t.category;
      const hdr = document.createElement("div");
      hdr.className = "cat-header " + catColors[t.category].css;
      hdr.textContent = t.category;
      list.appendChild(hdr);
    }
    const item = document.createElement("div");
    item.className = "target-item";
    item.dataset.index = DATA.indexOf(t);
    const rttClass = !t.total_rtt ? "rtt-mid" : t.total_rtt < 30 ? "rtt-good" : t.total_rtt < 80 ? "rtt-mid" : "rtt-bad";
    item.innerHTML = `
      <div>
        <div class="target-name">${t.name}</div>
        <div class="target-host">${t.host}</div>
      </div>
      <div class="target-meta">
        <div class="target-rtt ${rttClass}">${t.total_rtt ? t.total_rtt.toFixed(1) + " ms" : "N/A"}</div>
        <div class="target-hops">${t.hop_count} hops</div>
      </div>`;
    item.addEventListener("click", () => selectTarget(DATA.indexOf(t)));
    list.appendChild(item);
  });
}

// Draw ALL routes faintly on map
function drawAllRoutes() {
  allHopMarkers.clearLayers();
  DATA.forEach(t => {
    const pts = t.hops.filter(h => h.geo).map(h => [h.geo.lat, h.geo.lon]);
    if (pts.length > 1) {
      L.polyline(pts, {
        color: catColors[t.category]?.line || "#555",
        weight: 1,
        opacity: 0.15
      }).addTo(allHopMarkers);
    }
    // Endpoint marker
    const last = [...t.hops].reverse().find(h => h.geo);
    if (last) {
      L.circleMarker([last.geo.lat, last.geo.lon], {
        radius: 4,
        color: catColors[t.category]?.line || "#555",
        fillColor: catColors[t.category]?.line || "#555",
        fillOpacity: 0.6,
        weight: 1
      }).bindPopup(`<b>${t.name}</b><br>${t.host}<br>${last.geo.city}, ${last.geo.country}`).addTo(allHopMarkers);
    }
  });
}

function selectTarget(idx) {
  // Highlight sidebar
  document.querySelectorAll(".target-item").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(`.target-item[data-index="${idx}"]`).forEach(el => el.classList.add("active"));

  const t = DATA[idx];
  const color = catColors[t.category]?.line || "#00d4ff";

  // Draw route on map
  routeLayers.clearLayers();
  const pts = [];
  t.hops.forEach((h, i) => {
    if (!h.geo) return;
    pts.push([h.geo.lat, h.geo.lon]);
    const isLast = i === t.hops.length - 1 || (i === [...t.hops].reverse().findIndex(x => x.geo) ? false : true);
    L.circleMarker([h.geo.lat, h.geo.lon], {
      radius: i === 0 ? 7 : 5,
      color: color,
      fillColor: color,
      fillOpacity: 0.8,
      weight: 2
    }).bindPopup(
      `<b>Hop ${h.hop}</b><br>` +
      `${h.host}<br>` +
      `<span style="color:#00d4ff">${h.ip}</span><br>` +
      `${h.geo.city}, ${h.geo.region}, ${h.geo.country}<br>` +
      `RTT: <b>${h.rtt_ms ? h.rtt_ms.toFixed(1) + " ms" : "N/A"}</b><br>` +
      `<span style="color:#5a6c7d">${h.geo.isp}</span>`
    ).addTo(routeLayers);
  });

  if (pts.length > 1) {
    // Animated polyline
    L.polyline(pts, {color: color, weight: 3, opacity: 0.9}).addTo(routeLayers);
    // Glow effect
    L.polyline(pts, {color: color, weight: 8, opacity: 0.2}).addTo(routeLayers);
    map.fitBounds(L.latLngBounds(pts).pad(0.3));
  } else if (pts.length === 1) {
    map.setView(pts[0], 6);
  }

  // Detail panel
  const panel = document.getElementById("detail-panel");
  let maxRtt = Math.max(...t.hops.filter(h => h.rtt_ms).map(h => h.rtt_ms), 1);
  let rows = t.hops.map(h => {
    if (!h.ip) return `<tr><td class="hop-num">${h.hop}</td><td class="hop-timeout" colspan="4">* * * (timeout)</td></tr>`;
    const barW = h.rtt_ms ? Math.max(4, (h.rtt_ms / maxRtt) * 120) : 0;
    const barColor = !h.rtt_ms ? "#374151" : h.rtt_ms < 20 ? "#00ff88" : h.rtt_ms < 60 ? "#f59e0b" : "#ef4444";
    const loc = h.geo ? `${h.geo.city}${h.geo.city && h.geo.country ? ", " : ""}${h.geo.country}` : "";
    return `<tr>
      <td class="hop-num">${h.hop}</td>
      <td class="hop-ip">${h.ip}</td>
      <td class="hop-host" title="${h.host}">${h.host}</td>
      <td>${h.rtt_ms ? `<span class="rtt-bar" style="width:${barW}px;background:${barColor}"></span>${h.rtt_ms.toFixed(1)} ms` : "-"}</td>
      <td class="hop-loc">${loc}</td>
    </tr>`;
  }).join("");

  panel.innerHTML = `
    <div class="detail-title">${t.name} &mdash; ${t.host}</div>
    <table class="hop-table">
      <thead><tr><th>#</th><th>IP</th><th>Hostname</th><th>RTT</th><th>Location</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// Filter buttons
document.querySelectorAll(".filter-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    buildSidebar(btn.dataset.filter);
  });
});

// Init
buildSidebar("all");
drawAllRoutes();
</script>
</body>
</html>'''
    with open(HTML_FILE, "w") as f:
        f.write(html)
    print(f"Dashboard HTML written to {HTML_FILE}")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)
    def log_message(self, format, *args):
        pass  # suppress request logs


def main():
    print("=" * 60)
    print("  TRACEROUTE NETWORK DASHBOARD")
    print("=" * 60)

    if RESULTS_FILE.exists():
        print(f"\nFound existing results at {RESULTS_FILE}")
        print("Loading cached results (delete results.json to re-run)...")
        with open(RESULTS_FILE) as f:
            results = json.load(f)
    else:
        print(f"\nRunning traceroutes on {30} targets...")
        print("This will take a few minutes.\n")
        results = run_all_traceroutes()

    print("\nGenerating dashboard...")
    generate_html(results)

    print(f"\n>>> Dashboard ready at http://localhost:{PORT}")
    print(f">>> Press Ctrl+C to stop\n")

    with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
