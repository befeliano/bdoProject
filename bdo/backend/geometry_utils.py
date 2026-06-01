# backend/geometry_utils.py
"""
Geometri yardımcıları — bypass önerisinin gerçekçi olmasını sağlamak için.

Üç kritik fonksiyon:
  1. line_intersects_polygon  — çizgi binadan geçiyor mu?
  2. find_bypass_candidates   — mantıklı aday çiftleri (fiziksel yakın, graf uzak)
  3. validate_bypass          — bir bypass önerisi tüm kuralları sağlıyor mu?
"""
import math
from collections import defaultdict, deque


# ── Temel geometri ──────────────────────────────────────────────────────────

def _ccw(A, B, C):
    """Counter-clockwise yönelim testi (Cormen et al.)"""
    return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])


def segments_intersect(p1, p2, p3, p4):
    """İki çizgi parçası kesişiyor mu? (kesin değer, sayısal değil)"""
    return (_ccw(p1, p3, p4) != _ccw(p2, p3, p4) and
            _ccw(p1, p2, p3) != _ccw(p1, p2, p4))


def point_in_polygon(point, polygon):
    """Ray-casting algoritması (Wikipedia: Point in polygon)"""
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def line_intersects_polygon(p_start, p_end, polygon):
    """
    Bir doğru parçası bir poligonu kesiyor mu? (kenarlar veya iç)

    polygon: [(x1,y1), (x2,y2), ...]  — kapalı veya açık olabilir
    """
    # 1. Uçlardan biri poligonun içindeyse zaten kesişir
    if point_in_polygon(p_start, polygon) or point_in_polygon(p_end, polygon):
        return True

    # 2. Doğru, poligonun herhangi bir kenarıyla kesişiyor mu?
    n = len(polygon)
    for i in range(n):
        a = polygon[i]
        b = polygon[(i + 1) % n]
        if segments_intersect(p_start, p_end, a, b):
            return True
    return False


def euclidean_distance(p1, p2):
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def angle_between_vectors(v1, v2):
    """İki vektör arası açı (derece, 0-180)"""
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    mag1 = math.hypot(v1[0], v1[1])
    mag2 = math.hypot(v2[0], v2[1])
    if mag1 < 1e-9 or mag2 < 1e-9:
        return 0
    cos_angle = max(-1.0, min(1.0, dot / (mag1 * mag2)))
    return math.degrees(math.acos(cos_angle))


# ── Graf üzerinde mesafe (BFS) ──────────────────────────────────────────────

def graph_distance(adj_list, source, target, max_hops=20):
    """
    İki düğüm arası graf mesafesi (BFS, hop sayısı).
    max_hops içinde bulunamazsa -1 döner.
    """
    if source == target:
        return 0

    visited = {source}
    queue = deque([(source, 0)])

    while queue:
        node, dist = queue.popleft()
        if dist >= max_hops:
            continue
        for neighbor in adj_list.get(node, []):
            if neighbor == target:
                return dist + 1
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, dist + 1))

    return -1


# ── Aday üretimi ────────────────────────────────────────────────────────────

def find_bypass_candidates(nodes, edges, buildings,
                           bottleneck_edges,
                           max_bypass_length=250.0,
                           min_graph_distance=4,
                           min_angle_deg=20.0,
                           max_candidates=15):
    """
    Mantıklı bypass aday çiftleri üretir.

    Bir aday (A, B) çifti şu kriterleri sağlamalı:
      1. Fiziksel mesafe < max_bypass_length (gerçekçi uzunluk)
      2. Graf mesafesi >= min_graph_distance (zaten yakınsa bypass anlamsız)
      3. A→B çizgisi hiçbir binayı kesmemeli
      4. Mevcut bir kenarı tekrarlamamalı
      5. Darboğaz kenarlarına yakın olmalı (en azından bir aday)
      6. Mevcut kenarlarla çok dar açı yapmamalı

    Args:
        nodes: [{"id", "x", "y"}, ...]
        edges: [{"id", "from", "to"}, ...]
        buildings: [[(x,y), ...], ...]   yerel XY'de bina poligonları
        bottleneck_edges: [edge_id, ...]  öncelik için darboğaz id listesi

    Returns:
        [(node_a_id, node_b_id, distance, score), ...] — score düşük olan iyi
    """
    node_pos = {n["id"]: (n["x"], n["y"]) for n in nodes}

    # Komşuluk listesi (yönsüz, hop sayısı için)
    adj = defaultdict(set)
    existing_pairs = set()
    for e in edges:
        adj[e["from"]].add(e["to"])
        adj[e["to"]].add(e["from"])
        existing_pairs.add(frozenset([e["from"], e["to"]]))

    # Darboğaz kenarlarındaki düğümleri bul (öncelik için)
    bottleneck_nodes = set()
    edge_lookup = {e["id"]: e for e in edges}
    for eid in bottleneck_edges:
        if eid in edge_lookup:
            bottleneck_nodes.add(edge_lookup[eid]["from"])
            bottleneck_nodes.add(edge_lookup[eid]["to"])

    # Her düğümün mevcut kenarlarının vektörleri (açı kontrolü için)
    node_outgoing_vectors = defaultdict(list)
    for e in edges:
        a, b = e["from"], e["to"]
        if a not in node_pos or b not in node_pos:
            continue
        ax, ay = node_pos[a]
        bx, by = node_pos[b]
        node_outgoing_vectors[a].append((bx - ax, by - ay))
        node_outgoing_vectors[b].append((ax - bx, ay - by))

    candidates = []
    node_ids = list(node_pos.keys())
    n = len(node_ids)

    # Tüm çiftleri taramak O(n²); 200 düğüm için 20000 çift, kabul edilebilir
    for i in range(n):
        for j in range(i + 1, n):
            a_id = node_ids[i]
            b_id = node_ids[j]

            pa = node_pos[a_id]
            pb = node_pos[b_id]

            # 1. Uzunluk kontrolü
            length = euclidean_distance(pa, pb)
            if length > max_bypass_length or length < 30:
                continue

            # 2. Mevcut kenar mı?
            if frozenset([a_id, b_id]) in existing_pairs:
                continue

            # 3. Graf mesafesi (kısa devre değilse anlamsız)
            gd = graph_distance(adj, a_id, b_id, max_hops=min_graph_distance)
            if gd != -1 and gd < min_graph_distance:
                continue

            # 4. Açı kontrolü — yeni kenar mevcut kenarlarla çok dar açı yapmasın
            new_vec_a_to_b = (pb[0] - pa[0], pb[1] - pa[1])
            new_vec_b_to_a = (-new_vec_a_to_b[0], -new_vec_a_to_b[1])
            angle_ok = True
            for v in node_outgoing_vectors[a_id]:
                if angle_between_vectors(v, new_vec_a_to_b) < min_angle_deg:
                    angle_ok = False
                    break
            if angle_ok:
                for v in node_outgoing_vectors[b_id]:
                    if angle_between_vectors(v, new_vec_b_to_a) < min_angle_deg:
                        angle_ok = False
                        break
            if not angle_ok:
                continue

            # 5. Bina kesişimi (en pahalı kontrol, en sona)
            blocks_building = False
            for poly in buildings:
                if line_intersects_polygon(pa, pb, poly):
                    blocks_building = True
                    break
            if blocks_building:
                continue

            # Skor: kısa + darboğaza yakın = iyi (düşük skor)
            near_bottleneck = (a_id in bottleneck_nodes or
                               b_id in bottleneck_nodes)
            score = length * (0.5 if near_bottleneck else 1.0)

            candidates.append((a_id, b_id, round(length, 1), round(score, 1)))

    # Skora göre sırala, en iyi N adayı döndür
    candidates.sort(key=lambda c: c[3])
    return candidates[:max_candidates]


# ── Test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Basit test: 4 düğüm, biri diğerinden binayla ayrılmış
    nodes = [
        {"id": "A", "x": 0,   "y": 0},
        {"id": "B", "x": 200, "y": 0},
        {"id": "C", "x": 200, "y": 200},
        {"id": "D", "x": 0,   "y": 200},
    ]
    edges = [
        {"id": "E1", "from": "A", "to": "B"},
        {"id": "E2", "from": "B", "to": "C"},
        {"id": "E3", "from": "C", "to": "D"},
        {"id": "E4", "from": "D", "to": "A"},
    ]
    # A ile C arasında bir bina var (köşegeni kesmek için)
    buildings = [
        [(80, 80), (120, 80), (120, 120), (80, 120)],
    ]
    cands = find_bypass_candidates(nodes, edges, buildings,
                                   bottleneck_edges=["E1"])
    print("Aday çiftler (binayı kesmeyen):")
    for c in cands:
        print(f"  {c[0]} → {c[1]}: uzunluk {c[2]}m, skor {c[3]}")
    # A-C ve B-D köşegen olmalı; bina A-C'yi kesiyor → sadece B-D çıkmalı