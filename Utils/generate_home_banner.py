"""Generate the GeeUp home banner PNG using only the Python standard library."""

from __future__ import annotations

import math
import random
import struct
import zlib
from pathlib import Path
from typing import Iterable, Sequence


WIDTH = 1000
HEIGHT = 300
OUTPUT = Path(__file__).resolve().parents[1] / "assets" / "geeup_home_banner.png"

Color = tuple[int, int, int, int]
Point = tuple[float, float]


def clamp(value: float, low: int = 0, high: int = 255) -> int:
    return max(low, min(high, int(round(value))))


def make_canvas() -> list[list[Color]]:
    pixels: list[list[Color]] = []
    for y in range(HEIGHT):
        row: list[Color] = []
        vertical = y / max(1, HEIGHT - 1)
        for x in range(WIDTH):
            horizontal = x / max(1, WIDTH - 1)
            vignette = ((horizontal - 0.48) ** 2 + (vertical - 0.45) ** 2) ** 0.5
            r = 4 + 12 * (1 - vertical) + 10 * horizontal
            g = 13 + 20 * (1 - vertical)
            b = 29 + 34 * (1 - vertical) + 24 * horizontal
            r -= 18 * vignette
            g -= 18 * vignette
            b -= 12 * vignette
            row.append((clamp(r), clamp(g), clamp(b), 255))
        pixels.append(row)
    return pixels


def blend_pixel(pixels: list[list[Color]], x: int, y: int, color: Color) -> None:
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return
    sr, sg, sb, sa = color
    if sa <= 0:
        return
    dr, dg, db, da = pixels[y][x]
    alpha = sa / 255
    inv = 1 - alpha
    pixels[y][x] = (
        clamp(sr * alpha + dr * inv),
        clamp(sg * alpha + dg * inv),
        clamp(sb * alpha + db * inv),
        max(da, sa),
    )


def draw_line(
    pixels: list[list[Color]],
    p0: Point,
    p1: Point,
    color: Color,
    width: int = 1,
) -> None:
    x0, y0 = p0
    x1, y1 = p1
    steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0))))
    radius = max(0, width // 2)
    for step in range(steps + 1):
        t = step / steps
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        for yy in range(y - radius, y + radius + 1):
            for xx in range(x - radius, x + radius + 1):
                if (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius + 0.6:
                    blend_pixel(pixels, xx, yy, color)


def point_in_polygon(x: float, y: float, polygon: Sequence[Point]) -> bool:
    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def draw_polygon(pixels: list[list[Color]], polygon: Sequence[Point], color: Color) -> None:
    min_x = max(0, int(math.floor(min(x for x, _y in polygon))))
    max_x = min(WIDTH - 1, int(math.ceil(max(x for x, _y in polygon))))
    min_y = max(0, int(math.floor(min(y for _x, y in polygon))))
    max_y = min(HEIGHT - 1, int(math.ceil(max(y for _x, y in polygon))))
    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            if point_in_polygon(x + 0.5, y + 0.5, polygon):
                blend_pixel(pixels, x, y, color)


def rotated_rect(cx: float, cy: float, w: float, h: float, angle: float) -> list[Point]:
    cosine = math.cos(angle)
    sine = math.sin(angle)
    points = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return [
        (cx + x * cosine - y * sine, cy + x * sine + y * cosine)
        for x, y in points
    ]


def draw_stars(pixels: list[list[Color]]) -> None:
    rng = random.Random(42)
    for _ in range(180):
        x = rng.randrange(WIDTH)
        y = rng.randrange(0, 210)
        brightness = rng.randrange(70, 170)
        alpha = rng.randrange(40, 150)
        blend_pixel(pixels, x, y, (brightness, brightness + 10, 255, alpha))


def draw_earth(pixels: list[list[Color]]) -> None:
    cx, cy, radius = 500, 555, 510
    for y in range(92, HEIGHT):
        for x in range(WIDTH):
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            if radius - 18 <= dist <= radius + 12:
                glow = max(0, 1 - abs(dist - radius) / 16)
                blend_pixel(pixels, x, y, (80, 170, 255, int(110 * glow)))
            if dist < radius:
                edge = max(0, min(1, (radius - dist) / 70))
                shade = 0.55 + 0.45 * edge
                r = 18 + 34 * shade
                g = 82 + 70 * shade
                b = 147 + 72 * shade
                blend_pixel(pixels, x, y, (clamp(r), clamp(g), clamp(b), 235))

    land_shapes: Iterable[Sequence[Point]] = [
        [(115, 235), (205, 205), (300, 218), (345, 257), (290, 292), (155, 282)],
        [(470, 214), (560, 184), (660, 199), (742, 244), (690, 292), (520, 278)],
        [(780, 215), (874, 197), (930, 232), (902, 280), (805, 271)],
    ]
    for shape in land_shapes:
        draw_polygon(pixels, shape, (78, 132, 111, 90))

    for x in range(60, 940, 45):
        draw_line(pixels, (x, 222), (x + 105, 300), (76, 194, 255, 42), width=1)
    for y in range(222, 296, 17):
        draw_line(pixels, (0, y), (WIDTH, y + 8), (76, 194, 255, 38), width=1)

    for i, x in enumerate(range(610, 920, 48)):
        y = 208 + (i % 3) * 20
        draw_polygon(
            pixels,
            rotated_rect(x, y, 42, 22, -0.13),
            (56, 205, 229, 82),
        )


def draw_satellite(pixels: list[list[Color]]) -> None:
    angle = -0.36
    body = rotated_rect(475, 104, 58, 76, angle)
    draw_polygon(pixels, body, (218, 171, 49, 230))
    draw_polygon(pixels, rotated_rect(469, 96, 35, 48, angle), (238, 216, 129, 85))
    draw_polygon(pixels, rotated_rect(493, 121, 24, 58, angle), (112, 91, 74, 105))

    mast_left = rotated_rect(367, 93, 176, 8, angle)
    mast_right = rotated_rect(583, 115, 186, 8, angle)
    draw_polygon(pixels, mast_left, (222, 181, 55, 230))
    draw_polygon(pixels, mast_right, (222, 181, 55, 230))

    panel_specs = [(267, 73, 182, 54), (704, 139, 214, 58)]
    for cx, cy, w, h in panel_specs:
        panel = rotated_rect(cx, cy, w, h, angle)
        draw_polygon(pixels, panel, (20, 132, 222, 222))
        draw_polygon(pixels, rotated_rect(cx, cy, w - 10, h - 10, angle), (31, 210, 246, 78))
        for offset in range(-int(w // 2) + 18, int(w // 2), 24):
            cosine = math.cos(angle)
            sine = math.sin(angle)
            x0 = cx + offset * cosine + (-h / 2 + 4) * -sine
            y0 = cy + offset * sine + (-h / 2 + 4) * cosine
            x1 = cx + offset * cosine + (h / 2 - 4) * -sine
            y1 = cy + offset * sine + (h / 2 - 4) * cosine
            draw_line(pixels, (x0, y0), (x1, y1), (155, 235, 255, 90), width=1)
        for offset in range(-int(h // 2) + 12, int(h // 2), 14):
            cosine = math.cos(angle)
            sine = math.sin(angle)
            x0 = cx + (-w / 2 + 5) * cosine + offset * -sine
            y0 = cy + (-w / 2 + 5) * sine + offset * cosine
            x1 = cx + (w / 2 - 5) * cosine + offset * -sine
            y1 = cy + (w / 2 - 5) * sine + offset * cosine
            draw_line(pixels, (x0, y0), (x1, y1), (155, 235, 255, 80), width=1)

    draw_line(pixels, (501, 137), (566, 166), (240, 217, 160, 190), width=4)
    draw_line(pixels, (438, 128), (389, 161), (240, 217, 160, 190), width=4)
    draw_polygon(pixels, rotated_rect(578, 172, 88, 9, 0.36), (242, 243, 230, 230))
    draw_polygon(pixels, rotated_rect(378, 166, 96, 9, -0.23), (242, 243, 230, 230))


def draw_workflow_trace(pixels: list[list[Color]]) -> None:
    points = [(40, 260), (160, 238), (285, 248), (410, 224), (540, 234), (680, 214)]
    for start, end in zip(points, points[1:]):
        draw_line(pixels, start, end, (81, 220, 232, 105), width=3)
    for x, y in points:
        for radius in range(9, 0, -1):
            alpha = int(18 + (10 - radius) * 16)
            for yy in range(int(y - radius), int(y + radius) + 1):
                for xx in range(int(x - radius), int(x + radius) + 1):
                    if (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius:
                        blend_pixel(pixels, xx, yy, (103, 231, 241, alpha))


def encode_png(pixels: list[list[Color]]) -> bytes:
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, _a in row:
            raw.extend((r, g, b))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png.extend(chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)))
    png.extend(chunk(b"IDAT", zlib.compress(bytes(raw), level=9)))
    png.extend(chunk(b"IEND", b""))
    return bytes(png)


def main() -> None:
    pixels = make_canvas()
    draw_stars(pixels)
    draw_earth(pixels)
    draw_workflow_trace(pixels)
    draw_satellite(pixels)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(encode_png(pixels))
    print(OUTPUT)


if __name__ == "__main__":
    main()
