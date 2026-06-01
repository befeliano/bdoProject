# backend/traffic_sim.py
"""
BDO Proje — Trafik Simülatörü (YÖNLÜ + ZAMAN-TABANLI)

Mimarinin kalbi. Yapay verilen veya OSM'den çekilen yol ağı üzerinde:
  1. Sentetik trafik talebi üretir (preferential attachment hedef seçimi).
  2. Her aracı zaman-tabanlı Dijkstra ile en hızlı rotaya yönlendirir.
  3. Her edge için "araç yükü" hesaplar.
  4. Her kavşak için betweenness centrality hesaplar (darboğaz tespit).
  5. Önce/sonra karşılaştırması yapar.
  6. Gemini için darboğaz raporu formatlar.

Yönlü Graf:
  - TOON formatında EDGE'in 6. alanı yön: "BOTH" | "FWD" | "BWD"
  - "BOTH"   → from→to ve to→from iki yönlü kenar
  - "FWD"    → sadece from→to
  - "BWD"    → sadece to→from
  - Eski format (5 alan veya daha az) → varsayılan "BOTH"

Determinizm:
  random.Random(seed) sabitlenir → aynı graf + aynı seed = aynı sonuç.
  "Önce vs Sonra" karşılaştırmasının bilimsel olarak savunulabilir olmasının
  ön koşulu. Hocaya: "deterministik simülasyon, gürültü içermez."

Talep modeli:
  Origin: tamamen rastgele (her vatandaş her yerde yaşayabilir).
  Destination: derece-ağırlıklı (popüler kavşaklar daha çok hedef alır).
  Bu, gravity model'in basit bir uyarlamasıdır.
"""
from __future__ import annotations

import random
import math
import heapq
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

import networkx as nx


# ── Sabitler ──────────────────────────────────────────────────────────────────
RANDOM_SEED        = 42
DEFAULT_MAXSPEED   = 40        # km/h (şehir içi varsayılan)
DEFAULT_NUM_TRIPS  = 300       # api_server'dan kullanıcı belirleyecek

# Demo/eğitim projesi için makul betweenness örnekleme limiti.
# >500 düğüm için exact betweenness pahalı, k-örnekleme yeterli.
BETWEENNESS_EXACT_THRESHOLD = 500
BETWEENNESS_SAMPLE_K        = 300


# ─────────────────────────────────────────────────────────────────────────────
#  TOON PARSER
# ─────────────────────────────────────────────────────────────────────────────
def parse_toon(toon_data: str) -> Tuple[List[Dict], List[Dict]]:
    """
    TOON formatını yapılandırılmış nodes + edges listesine ayrıştırır.

    NODE;id;x;y                         → eski (yönsüz, geo yok)
    NODE;id;x;y;lat;lon                 → OSM (geo var)
    EDGE;id;from;to                     → eski (yönsüz)
    EDGE;id;from;to;name                → ismi olan
    EDGE;id;from;to;name;direction      → yönlü (yeni format)

    Returns:
        nodes: [{"id","x","y", lat?, lon?}]
        edges: [{"id","from","to","name","direction","oneway","maxspeed"}]
                direction: "BOTH" | "FWD" | "BWD"
                oneway:    bool   (FWD/BWD ise True)
                maxspeed:  km/h   (varsayılan DEFAULT_MAXSPEED)
    """
    nodes: List[Dict] = []
    edges: List[Dict] = []

    for raw in toon_data.strip().split("\n"):
        line = raw.strip()
        if not line or line in ("NETWORK_START", "NETWORK_END"):
            continue

        parts = line.split(";")
        tag = parts[0]

        try:
            if tag == "NODE" and len(parts) >= 4:
                node = {
                    "id": parts[1],
                    "x":  float(parts[2]),
                    "y":  float(parts[3]),
                }
                if len(parts) >= 6:
                    try:
                        node["lat"] = float(parts[4])
                        node["lon"] = float(parts[5])
                    except ValueError:
                        pass
                nodes.append(node)

            elif tag == "EDGE" and len(parts) >= 4:
                eid, a, b = parts[1], parts[2], parts[3]
                if a == b:
                    continue  # self-loop atılır

                name      = parts[4].strip() if len(parts) >= 5 else ""
                direction = parts[5].strip().upper() if len(parts) >= 6 else "BOTH"
                if direction not in ("BOTH", "FWD", "BWD"):
                    direction = "BOTH"   # geçersiz değer → güvenli varsayılan

                edges.append({
                    "id":        eid,
                    "from":      a,
                    "to":        b,
                    "name":      name,
                    "direction": direction,
                    "oneway":    direction != "BOTH",
                    "maxspeed":  DEFAULT_MAXSPEED,
                })
        except (ValueError, IndexError):
            # Bozuk satır → simülasyonu durdurma, atla
            continue

    return nodes, edges


# ─────────────────────────────────────────────────────────────────────────────
#  YÖNLÜ KOMŞULUK LİSTESİ
# ─────────────────────────────────────────────────────────────────────────────
def _build_directed_adj(
    nodes: List[Dict], edges: List[Dict]
) -> Tuple[Dict[str, List], Dict[str, Dict]]:
    """
    Yönlü komşuluk listesi + edge meta verisi kurar.

    direction kuralları:
      BOTH → from→to ve to→from
      FWD  → from→to
      BWD  → to→from

    Returns:
        adj: { node_id: [(neighbor_id, travel_time_sec, edge_id), ...] }
        edge_meta: { edge_id: {...} }
    """
    pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
    adj: Dict[str, List] = defaultdict(list)
    edge_meta: Dict[str, Dict] = {}

    for e in edges:
        a, b = e["from"], e["to"]
        if a not in pos or b not in pos:
            continue

        ax, ay = pos[a]
        bx, by = pos[b]
        length = math.hypot(bx - ax, by - ay)
        if length < 1e-6:
            continue

        speed_ms = e["maxspeed"] * 1000.0 / 3600.0   # km/h → m/s
        ttime    = length / speed_ms                  # saniye

        direction = e.get("direction", "BOTH")

        if direction == "BOTH":
            adj[a].append((b, ttime, e["id"]))
            adj[b].append((a, ttime, e["id"]))
        elif direction == "FWD":
            adj[a].append((b, ttime, e["id"]))
        elif direction == "BWD":
            adj[b].append((a, ttime, e["id"]))

        edge_meta[e["id"]] = {
            "from":        a,
            "to":          b,
            "name":        e.get("name", ""),
            "direction":   direction,
            "length":      length,
            "maxspeed":    e["maxspeed"],
            "travel_time": ttime,
        }

    return adj, edge_meta


# ─────────────────────────────────────────────────────────────────────────────
#  DİJKSTRA (zaman-ağırlıklı)
# ─────────────────────────────────────────────────────────────────────────────
def _dijkstra(adj: Dict[str, List], source: str
              ) -> Tuple[Dict[str, float], Dict[str, Optional[Tuple]]]:
    """
    Tek-kaynaklı en kısa süre. Ağırlık = travel_time.
    Returns: (dist, prev_edge)
    """
    dist: Dict[str, float] = {source: 0.0}
    prev_edge: Dict[str, Optional[Tuple]] = {source: None}
    heap = [(0.0, source)]

    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        for v, w, eid in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v]      = nd
                prev_edge[v] = (u, eid)
                heapq.heappush(heap, (nd, v))
    return dist, prev_edge


def _reconstruct_edges(prev_edge: Dict, target: str) -> List[str]:
    """Hedef düğümden geri yürüyüp yol üzerindeki edge ID'lerini topla."""
    out = []
    cur = target
    while prev_edge.get(cur) is not None:
        parent, eid = prev_edge[cur]
        out.append(eid)
        cur = parent
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  TALEP MODELİ: Preferential Attachment
# ─────────────────────────────────────────────────────────────────────────────
def _generate_od_pairs(
    nodes: List[Dict],
    edges: List[Dict],
    num_trips: int,
    rng: random.Random,
) -> List[Tuple[str, str]]:
    """
    Origin-Destination çiftleri üretir.

    Origin: uniform rastgele.
    Destination: derece-ağırlıklı (gravity model benzeri). Yüksek dereceli
                 kavşaklar daha çok hedef alır → gerçekçi şehir trafiği.

    Hocanın "neden bu dağılım?" sorusuna cevap: insanlar rastgele yerlerde
    yaşar (origin uniform), ama merkezi kavşaklara/caddelere giderler
    (destination derece-ağırlıklı).
    """
    node_ids = [n["id"] for n in nodes]
    if len(node_ids) < 2:
        return []

    # Yönsüz derece (her edge ucu sayılır — yön bilgisinden bağımsız "merkezilik"
    # ölçüsü; burada amaç hedef seçimi, akış değil)
    degree: Dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]]   += 1

    # +1 → izole düğümler bile %küçük şansa sahip olsun
    weights = [degree[nid] + 1 for nid in node_ids]

    pairs: List[Tuple[str, str]] = []
    attempts = 0
    max_attempts = num_trips * 5   # sonsuz döngü koruması

    while len(pairs) < num_trips and attempts < max_attempts:
        attempts += 1
        src = rng.choice(node_ids)
        dst = rng.choices(node_ids, weights=weights, k=1)[0]
        if src == dst:
            continue
        pairs.append((src, dst))

    return pairs


# ─────────────────────────────────────────────────────────────────────────────
#  BETWEENNESS CENTRALITY (yönlü)
# ─────────────────────────────────────────────────────────────────────────────
def _compute_betweenness(nodes: List[Dict], edges: List[Dict],
                         seed: int) -> Dict[str, float]:
    """
    Yönlü graf üzerinde betweenness centrality. Büyük graflarda
    örnekleme yapar (BETWEENNESS_EXACT_THRESHOLD üstü için).
    """
    DG = nx.DiGraph()
    for n in nodes:
        DG.add_node(n["id"])

    for e in edges:
        a, b = e["from"], e["to"]
        # Ağırlık olarak travel_time kullan (mesafe + hız)
        # Hesabı _build_directed_adj'daki ile tutarlı yapmak için tekrarla
        try:
            ax, ay = next(n["x"] for n in nodes if n["id"] == a), \
                     next(n["y"] for n in nodes if n["id"] == a)
            bx, by = next(n["x"] for n in nodes if n["id"] == b), \
                     next(n["y"] for n in nodes if n["id"] == b)
        except StopIteration:
            continue
        length = math.hypot(bx - ax, by - ay)
        if length < 1e-6:
            continue
        speed_ms = e.get("maxspeed", DEFAULT_MAXSPEED) * 1000.0 / 3600.0
        ttime = length / speed_ms

        direction = e.get("direction", "BOTH")
        if direction in ("BOTH", "FWD"):
            DG.add_edge(a, b, weight=ttime)
        if direction in ("BOTH", "BWD"):
            DG.add_edge(b, a, weight=ttime)

    if DG.number_of_nodes() == 0:
        return {}

    if DG.number_of_nodes() > BETWEENNESS_EXACT_THRESHOLD:
        bw = nx.betweenness_centrality(
            DG, k=min(BETWEENNESS_SAMPLE_K, DG.number_of_nodes()),
            weight="weight", seed=seed
        )
    else:
        bw = nx.betweenness_centrality(DG, weight="weight")

    return {k: round(v, 4) for k, v in bw.items()}


# ─────────────────────────────────────────────────────────────────────────────
#  ANA SİMÜLATÖR
# ─────────────────────────────────────────────────────────────────────────────
def simulate_traffic(
    nodes: List[Dict],
    edges: List[Dict],
    num_trips: int = DEFAULT_NUM_TRIPS,
    seed: int = RANDOM_SEED,
) -> Dict:
    """
    Tam simülasyon. Returns zenginleştirilmiş sonuç dict'i.

    Returns:
      {
        "edge_loads":       {edge_id: load, ...},
        "max_load":         int,
        "avg_load":         float,
        "total_trips":      int,           # rota bulunan
        "failed_trips":     int,           # yön nedeniyle ulaşılamayan
        "demand_count":     int,           # üretilen toplam OD çifti
        "avg_path_length":  float,         # ortalama hop sayısı
        "node_betweenness": {node_id: bw_norm, ...},
        "top_bottleneck_edges": [
            {"edge_id","from","to","name","direction","load"}, ... (5 adet)
        ],
        "top_bottleneck_nodes": [
            {"node_id","betweenness","degree"}, ... (5 adet)
        ],
        "seed": int,
      }
    """
    rng = random.Random(seed)
    adj, edge_meta = _build_directed_adj(nodes, edges)

    # Boş durumlar
    if not edge_meta or len(nodes) < 2:
        return _empty_result(seed, num_trips)

    # ── Trafik talebi ─────────────────────────────────────────────────────
    od_pairs = _generate_od_pairs(nodes, edges, num_trips, rng)
    demand_count = len(od_pairs)

    # ── Rota ataması ──────────────────────────────────────────────────────
    edge_loads: Dict[str, int] = defaultdict(int)
    successful = 0
    failed = 0
    total_hops = 0
    origin_cache: Dict[str, Tuple[Dict, Dict]] = {}

    for src, dst in od_pairs:
        if src not in origin_cache:
            origin_cache[src] = _dijkstra(adj, src)
        dist, prev_edge = origin_cache[src]

        if dst not in dist:
            failed += 1
            continue

        path_edges = _reconstruct_edges(prev_edge, dst)
        if not path_edges:
            failed += 1
            continue

        successful += 1
        total_hops += len(path_edges)
        for eid in path_edges:
            edge_loads[eid] += 1

    # Yüklenmemiş edge'ler de 0 olarak raporlansın (frontend gradient'i için)
    for eid in edge_meta:
        edge_loads.setdefault(eid, 0)

    # ── Özet metrikler ────────────────────────────────────────────────────
    loads = list(edge_loads.values())
    max_load = max(loads) if loads else 0
    avg_load = round(sum(loads) / len(loads), 2) if loads else 0.0
    avg_path = round(total_hops / successful, 2) if successful else 0.0

    # ── Top darboğaz edge'ler ─────────────────────────────────────────────
    edge_info = []
    for eid, meta in edge_meta.items():
        edge_info.append({
            "edge_id":   eid,
            "from":      meta["from"],
            "to":        meta["to"],
            "name":      meta["name"],
            "direction": meta["direction"],
            "load":      edge_loads.get(eid, 0),
        })
    edge_info.sort(key=lambda x: x["load"], reverse=True)
    top_bottleneck_edges = edge_info[:5]

    # ── Betweenness + top kavşaklar ───────────────────────────────────────
    bw = _compute_betweenness(nodes, edges, seed)

    # Yönsüz derece (görselde nokta boyutu için)
    degree: Dict[str, int] = defaultdict(int)
    for e in edges:
        degree[e["from"]] += 1
        degree[e["to"]]   += 1

    node_info = [
        {
            "node_id":     nid,
            "betweenness": bw_val,
            "degree":      degree.get(nid, 0),
        }
        for nid, bw_val in bw.items()
    ]
    node_info.sort(key=lambda x: x["betweenness"], reverse=True)
    top_bottleneck_nodes = node_info[:5]

    return {
        "edge_loads":           dict(edge_loads),
        "max_load":             max_load,
        "avg_load":             avg_load,
        "total_trips":          successful,
        "failed_trips":         failed,
        "demand_count":         demand_count,
        "avg_path_length":      avg_path,
        "node_betweenness":     bw,
        "top_bottleneck_edges": top_bottleneck_edges,
        "top_bottleneck_nodes": top_bottleneck_nodes,
        "seed":                 seed,
    }


def _empty_result(seed: int, num_trips: int) -> Dict:
    return {
        "edge_loads": {},
        "max_load": 0, "avg_load": 0.0,
        "total_trips": 0, "failed_trips": 0,
        "demand_count": 0, "avg_path_length": 0.0,
        "node_betweenness": {},
        "top_bottleneck_edges": [],
        "top_bottleneck_nodes": [],
        "seed": seed,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ÖNCE / SONRA KARŞILAŞTIRMA
# ─────────────────────────────────────────────────────────────────────────────
def compare_simulations(before: Dict, after: Dict) -> Dict:
    """Ana metrik: max_load'un yüzde azalması (pozitif = iyileşme)."""
    def pct_drop(old: float, new: float) -> float:
        if old <= 0:
            return 0.0
        return round((old - new) / old * 100, 2)

    return {
        "max_load_before":          before["max_load"],
        "max_load_after":           after["max_load"],
        "max_load_improvement_pct": pct_drop(before["max_load"], after["max_load"]),

        "avg_load_before":          before["avg_load"],
        "avg_load_after":           after["avg_load"],
        "avg_load_improvement_pct": pct_drop(before["avg_load"], after["avg_load"]),

        "avg_path_before":          before["avg_path_length"],
        "avg_path_after":           after["avg_path_length"],
        "avg_path_improvement_pct": pct_drop(
            before["avg_path_length"], after["avg_path_length"]
        ),

        "failed_before":            before["failed_trips"],
        "failed_after":              after["failed_trips"],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI İÇİN DARBOĞAZ ÖZETİ
# ─────────────────────────────────────────────────────────────────────────────
def format_bottleneck_summary_for_llm(sim: Dict) -> str:
    """Gemini'a gönderilecek insan-okunur darboğaz raporu."""
    lines = []
    lines.append(
        f"Simülasyon: {sim['total_trips']} araç başarıyla rota aldı "
        f"({sim['failed_trips']} araç ulaşılamayan hedef → tek-yön kuralı)."
    )
    lines.append(
        f"Max yol yükü: {sim['max_load']} araç, ortalama: {sim['avg_load']}."
    )
    lines.append(
        f"Ortalama yol uzunluğu: {sim['avg_path_length']} segment."
    )
    lines.append("")
    lines.append("EN YOĞUN 5 YOL (rahatlatılması gereken yollar):")
    for i, e in enumerate(sim["top_bottleneck_edges"], 1):
        name = f" ({e['name']})" if e["name"] else ""
        dir_marker = ""
        if e["direction"] == "FWD":
            dir_marker = " [tek yön →]"
        elif e["direction"] == "BWD":
            dir_marker = " [tek yön ←]"
        lines.append(
            f"  {i}. {e['from']} → {e['to']}{name}{dir_marker} — {e['load']} araç"
        )
    lines.append("")
    lines.append("EN KRİTİK 5 KAVŞAK (yüksek betweenness centrality):")
    for i, n in enumerate(sim["top_bottleneck_nodes"], 1):
        lines.append(
            f"  {i}. {n['node_id']} — betweenness: {n['betweenness']}, "
            f"derece: {n['degree']}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  TEST
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample_toon = """NETWORK_START
NODE;N1;0;0
NODE;N2;100;0
NODE;N3;200;0
NODE;N4;100;100
NODE;N5;200;100
EDGE;E1;N1;N2;;BOTH
EDGE;E2;N2;N3;;BOTH
EDGE;E3;N2;N4;Atatürk Cd;FWD
EDGE;E4;N4;N5;;BOTH
EDGE;E5;N3;N5;İnönü Cd;BOTH
NETWORK_END
"""
    nodes, edges = parse_toon(sample_toon)
    print(f"Parsed: {len(nodes)} düğüm, {len(edges)} kenar")
    print(f"Yön dağılımı: {[e['direction'] for e in edges]}")

    sim = simulate_traffic(nodes, edges, num_trips=200)
    print(f"\nMax load: {sim['max_load']}")
    print(f"Avg load: {sim['avg_load']}")
    print(f"Successful: {sim['total_trips']}, Failed: {sim['failed_trips']}")
    print(f"\nTop bottleneck edges:")
    for e in sim["top_bottleneck_edges"]:
        print(f"  {e['edge_id']}: {e['from']}→{e['to']} ({e['direction']}) = {e['load']}")

    print(f"\n--- LLM PROMPT ---")
    print(format_bottleneck_summary_for_llm(sim))

    # Determinizm kontrolü
    sim2 = simulate_traffic(nodes, edges, num_trips=200)
    assert sim["max_load"] == sim2["max_load"], "Determinism BROKEN!"
    print(f"\n✓ Determinism ok: max={sim['max_load']} her seferinde aynı")