from math import hypot


def _to_pixels(
    x: float,
    y: float,
    pixel_size_x: float,
    pixel_size_y: float,
) -> tuple[float, float]:
    # Measuring hits in pixel space keeps the grab tolerance constant on screen
    # regardless of zoom. When pixel sizes are unknown (0.0, e.g. in tests) we
    # fall back to data units so the math still degrades gracefully.
    scale_x = pixel_size_x if pixel_size_x > 0.0 else 1.0
    scale_y = pixel_size_y if pixel_size_y > 0.0 else 1.0
    return x / scale_x, y / scale_y


def point_distance(
    px: float,
    py: float,
    qx: float,
    qy: float,
    pixel_size_x: float,
    pixel_size_y: float,
) -> float:
    ax, ay = _to_pixels(px, py, pixel_size_x, pixel_size_y)
    bx, by = _to_pixels(qx, qy, pixel_size_x, pixel_size_y)
    return hypot(ax - bx, ay - by)


def segment_distance(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    pixel_size_x: float,
    pixel_size_y: float,
) -> float:
    """Shortest pixel-space distance from point P to segment A→B."""
    p = _to_pixels(px, py, pixel_size_x, pixel_size_y)
    a = _to_pixels(ax, ay, pixel_size_x, pixel_size_y)
    b = _to_pixels(bx, by, pixel_size_x, pixel_size_y)
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    foot_x = a[0] + t * dx
    foot_y = a[1] + t * dy
    return hypot(p[0] - foot_x, p[1] - foot_y)


def nearest_vertex(
    px: float,
    py: float,
    layout: list[tuple[float, float]],
    pixel_size_x: float,
    pixel_size_y: float,
    max_pixels: float,
) -> int | None:
    """Index of the vertex within max_pixels of (px, py), nearest first."""
    best_index: int | None = None
    best_distance = max_pixels
    for index, (vx, vy) in enumerate(layout):
        distance = point_distance(px, py, vx, vy, pixel_size_x, pixel_size_y)
        if distance <= best_distance:
            best_distance = distance
            best_index = index
    return best_index


def body_distance(
    px: float,
    py: float,
    layout: list[tuple[float, float]],
    pixel_size_x: float,
    pixel_size_y: float,
) -> float:
    """Shortest pixel-space distance from (px, py) to any segment of the path."""
    if len(layout) == 1:
        vx, vy = layout[0]
        return point_distance(px, py, vx, vy, pixel_size_x, pixel_size_y)
    distances = [
        segment_distance(px, py, layout[i][0], layout[i][1], layout[i + 1][0], layout[i + 1][1], pixel_size_x, pixel_size_y)
        for i in range(len(layout) - 1)
    ]
    return min(distances)
