# backend/sumo_converter.py
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import subprocess
import shutil
import random


# ── Yardımcılar ──────────────────────────────────────────────────────────────
def _indent(elem):
    rough = ET.tostring(elem, 'utf-8')
    return minidom.parseString(rough).toprettyxml(indent="  ")


def _find_netconvert():
    found = shutil.which("netconvert")
    if found:
        return found
    win_paths = [
        r"D:\Eclipse\bin\netconvert.exe",
        r"C:\Program Files (x86)\Eclipse\Sumo\bin\netconvert.exe",
        r"C:\Program Files\Eclipse\Sumo\bin\netconvert.exe",
        r"C:\sumo\bin\netconvert.exe",
    ]
    unix_paths = [
        "/usr/bin/netconvert",
        "/usr/local/bin/netconvert",
        "/opt/sumo/bin/netconvert",
    ]
    for p in win_paths + unix_paths:
        if os.path.isfile(p):
            return p
    return None


# ── TOON'u parse et (route üretimi için lazım) ────────────────────────────────
def _parse_toon_lite(toon_data: str):
    """Sadece node/edge listesi — traffic_sim'deki ile aynı temel."""
    nodes, edges = [], []
    for line in toon_data.strip().split("\n"):
        parts = line.strip().split(";")
        if len(parts) < 2:
            continue
        if parts[0] == "NODE" and len(parts) >= 4:
            nodes.append({"id": parts[1], "x": float(parts[2]), "y": float(parts[3])})
        elif parts[0] == "EDGE" and len(parts) >= 4:
            direction = parts[5].strip().upper() if len(parts) >= 6 else "BOTH"
            if direction not in ("BOTH", "FWD", "BWD"):
                direction = "BOTH"
            edges.append({
                "id": parts[1], "from": parts[2], "to": parts[3],
                "direction": direction,
            })
    return nodes, edges


# ── 1. Network XML üretimi (mevcut, küçük güncellemelerle) ───────────────────
def _write_network_xmls(nodes, edges, folder):
    """nod.xml ve edg.xml dosyalarını yazar."""
    nod_path = os.path.join(folder, "network.nod.xml")
    edg_path = os.path.join(folder, "network.edg.xml")

    # nod.xml
    nodes_root = ET.Element("nodes")
    for n in nodes:
        ET.SubElement(
            nodes_root, "node",
            id=n["id"], x=str(n["x"]), y=str(n["y"]),
            type="priority",
        )
    with open(nod_path, "w", encoding="utf-8") as f:
        f.write(_indent(nodes_root))

    # edg.xml — yön bilgisini kullan
    edges_root = ET.Element("edges")
    for e in edges:
        direction = e.get("direction", "BOTH")
        if direction in ("BOTH", "FWD"):
            ET.SubElement(
                edges_root, "edge",
                id=e["id"],
                **{"from": e["from"], "to": e["to"],
                   "numLanes": "2", "speed": "13.89"}
            )
        if direction in ("BOTH", "BWD"):
            # Çift yönlü → ters yön için ayrı edge (BWD ise sadece bu)
            ET.SubElement(
                edges_root, "edge",
                id=f"{e['id']}_r" if direction == "BOTH" else e["id"],
                **{"from": e["to"], "to": e["from"],
                   "numLanes": "2", "speed": "13.89"}
            )
    with open(edg_path, "w", encoding="utf-8") as f:
        f.write(_indent(edges_root))

    return nod_path, edg_path


def _run_netconvert(nod_path, edg_path, net_path):
    """netconvert ile .net.xml üretir."""
    nc = _find_netconvert()
    if not nc:
        return False, "netconvert bulunamadı", None

    cmd = [
        nc,
        "--node-files", nod_path,
        "--edge-files", edg_path,
        "--output-file", net_path,
        "--no-warnings",
        "--no-turnarounds",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return True, None, nc
        err = (proc.stderr or proc.stdout).strip()[:300]
        return False, err, nc
    except Exception as e:
        return False, str(e), nc


# ── 2. Route dosyası üretimi (TraCI için gerekli) ────────────────────────────
def _write_routes(nodes, edges, folder, num_trips=300, seed=42, sim_duration=600):
    """
    network.rou.xml üretir — sim_duration saniye boyunca num_trips araç,
    rastgele origin-destination çiftleri.

    SUMO routes formatı:
      <routes>
        <vType id="car" accel="2.6" decel="4.5" maxSpeed="13.89" .../>
        <trip id="t0" depart="0.0" from="E5" to="E120"/>
        <trip id="t1" depart="2.3" from="E40" to="E80"/>
        ...
      </routes>

    SUMO trip'leri otomatik route'lar (kendi içinde Dijkstra koşturur).
    """
    rng = random.Random(seed)

    # Edge ID listesi (BOTH için _r de var ama trip'ler ana ID üzerinden)
    edge_ids = [e["id"] for e in edges]
    if len(edge_ids) < 2:
        return None

    rou_path = os.path.join(folder, "network.rou.xml")
    root = ET.Element("routes")

    # Araç tipi: standart şehir arabası
    ET.SubElement(
        root, "vType",
        id="car",
        accel="2.6", decel="4.5", sigma="0.5",
        length="5.0", minGap="2.5", maxSpeed="13.89",
        guiShape="passenger",
    )

    # num_trips araç, uniform aralıklarla
    interval = sim_duration / num_trips
    for i in range(num_trips):
        depart_time = round(i * interval, 2)
        # Rastgele origin/destination edge'leri (aynı olmamalı)
        from_e = rng.choice(edge_ids)
        to_e = rng.choice(edge_ids)
        attempts = 0
        while to_e == from_e and attempts < 10:
            to_e = rng.choice(edge_ids)
            attempts += 1

        ET.SubElement(
            root, "trip",
            id=f"t{i}",
            type="car",
            depart=str(depart_time),
            **{"from": from_e, "to": to_e},
        )

    with open(rou_path, "w", encoding="utf-8") as f:
        f.write(_indent(root))

    return rou_path


# ── 3. SUMO config dosyası ───────────────────────────────────────────────────
def _write_sumocfg(folder, sim_duration=600):
    """network.sumocfg üretir — sumo komutuna verilecek config."""
    cfg_path = os.path.join(folder, "network.sumocfg")
    root = ET.Element("configuration")

    inp = ET.SubElement(root, "input")
    ET.SubElement(inp, "net-file", value="network.net.xml")
    ET.SubElement(inp, "route-files", value="network.rou.xml")

    time = ET.SubElement(root, "time")
    ET.SubElement(time, "begin", value="0")
    ET.SubElement(time, "end", value=str(sim_duration))

    processing = ET.SubElement(root, "processing")
    ET.SubElement(processing, "ignore-route-errors", value="true")
    ET.SubElement(processing, "time-to-teleport", value="120")  # tıkanırsa ışınla

    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(_indent(root))

    return cfg_path


# ── ANA FONKSİYON (genişletilmiş) ────────────────────────────────────────────
def export_to_sumo_xml(
    toon_data: str,
    output_folder: str = "sumo_files",
    num_trips: int = 300,
    seed: int = 42,
    sim_duration: int = 600,
    generate_routes: bool = True,
) -> dict:
    """
    TOON → SUMO dosya seti üretir.

    Üretilen dosyalar (output_folder içinde):
      - network.nod.xml
      - network.edg.xml
      - network.net.xml            (netconvert ile)
      - network.rou.xml            (eğer generate_routes=True)
      - network.sumocfg            (eğer generate_routes=True)

    Returns:
      {
        "folder":          "sumo_files",
        "nod_xml":         True,
        "edg_xml":         True,
        "net_xml":         True/False,
        "rou_xml":         True/False,
        "sumocfg":         True/False,
        "netconvert_path": "...",
        "error":           None/"...",
      }
    """
    os.makedirs(output_folder, exist_ok=True)

    result = {
        "folder": output_folder,
        "nod_xml": False, "edg_xml": False, "net_xml": False,
        "rou_xml": False, "sumocfg": False,
        "netconvert_path": None, "error": None,
    }

    # 1. Parse
    nodes, edges = _parse_toon_lite(toon_data)
    if not nodes or not edges:
        result["error"] = "TOON boş veya ayrıştırılamadı"
        return result

    # 2. nod/edg
    nod_path, edg_path = _write_network_xmls(nodes, edges, output_folder)
    result["nod_xml"] = True
    result["edg_xml"] = True
    print(f"[SUMO] .nod.xml ({len(nodes)}) ve .edg.xml ({len(edges)}) → {output_folder}")

    # 3. netconvert → net.xml
    net_path = os.path.join(output_folder, "network.net.xml")
    ok, err, nc = _run_netconvert(nod_path, edg_path, net_path)
    result["netconvert_path"] = nc
    if ok:
        result["net_xml"] = True
        print(f"[SUMO] ✅ .net.xml → {net_path}")
    else:
        result["error"] = err
        print(f"[SUMO] ❌ netconvert: {err}")
        return result   # net.xml yoksa route'ların anlamı kalmaz

    # 4. routes + sumocfg
    if generate_routes:
        rou_path = _write_routes(nodes, edges, output_folder,
                                 num_trips=num_trips, seed=seed,
                                 sim_duration=sim_duration)
        if rou_path:
            result["rou_xml"] = True
            print(f"[SUMO] .rou.xml ({num_trips} araç, {sim_duration}s) → {rou_path}")

        cfg_path = _write_sumocfg(output_folder, sim_duration=sim_duration)
        result["sumocfg"] = True
        print(f"[SUMO] .sumocfg → {cfg_path}")

    return result