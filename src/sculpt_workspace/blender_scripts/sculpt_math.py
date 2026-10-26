"""Dependency-free vector and camera math.

Imported both by the server (for validation and image-ray derivation) and by
scripts running inside Blender, so it must stay pure Python.

Conventions
-----------
* World frame: Blender's Z-up. The default viewing frame looks at the front
  from -Y: ``front = (0, -1, 0)`` points from the sculpture toward the viewer.
* Matrices are row-major 4x4 nested lists; points are column vectors.
* Cameras follow Blender: they look down their local -Z with +Y up.
* Image coordinates are normalised with the origin at the TOP-LEFT corner:
  ``u`` grows right, ``v`` grows down, both in [0, 1].
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vec = tuple[float, float, float]
Mat4 = list[list[float]]

SENSOR_WIDTH_MM = 36.0
STANDARD_LENS_MM = 50.0
ORTHO_VIEWS = ("front", "right", "back", "left", "top")
PERSPECTIVE_ELEVATION_DEG = 20.0


def add(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Sequence[float], s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def length(a: Sequence[float]) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Sequence[float]) -> Vec:
    n = length(a)
    if n < 1e-12:
        raise ValueError("cannot normalise a zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def frame_axes(front: Sequence[float], up: Sequence[float]) -> tuple[Vec, Vec, Vec]:
    """Return orthonormal (front, up, right) for a viewing frame.

    For the default frame, right = +X, matching Blender's Right view.
    """
    f = normalize(front)
    r = normalize(cross(up, f))
    u = cross(f, r)
    return f, u, r


def view_direction(name: str, front: Sequence[float], up: Sequence[float]) -> tuple[Vec, Vec]:
    """Direction from the target toward the camera, and the camera's up vector."""
    f, u, r = frame_axes(front, up)
    if name == "front":
        return f, u
    if name == "back":
        return scale(f, -1.0), u
    if name == "right":
        return r, u
    if name == "left":
        return scale(r, -1.0), u
    if name == "top":
        return u, scale(f, -1.0)
    if name == "perspective":
        el = math.radians(PERSPECTIVE_ELEVATION_DEG)
        horiz = normalize(add(f, r))
        return normalize(add(scale(horiz, math.cos(el)), scale(u, math.sin(el)))), u
    raise ValueError(f"unknown view {name!r}")


def look_at(location: Sequence[float], target: Sequence[float], up: Sequence[float]) -> Mat4:
    """Camera-to-world matrix for a camera at `location` looking at `target`."""
    z = normalize(sub(location, target))
    x_raw = cross(up, z)
    if length(x_raw) < 1e-9:
        # Up is parallel to the view direction; pick any perpendicular axis.
        alt = (0.0, 1.0, 0.0) if abs(z[1]) < 0.9 else (1.0, 0.0, 0.0)
        x_raw = cross(alt, z)
    x = normalize(x_raw)
    y = cross(z, x)
    return [
        [x[0], y[0], z[0], float(location[0])],
        [x[1], y[1], z[1], float(location[1])],
        [x[2], y[2], z[2], float(location[2])],
        [0.0, 0.0, 0.0, 1.0],
    ]


def projection_matrix(
    cam_type: str,
    width: int,
    height: int,
    clip_start: float,
    clip_end: float,
    lens_mm: float = STANDARD_LENS_MM,
    ortho_scale: float = 1.0,
    sensor_mm: float = SENSOR_WIDTH_MM,
) -> Mat4:
    """OpenGL-style projection matching Blender's AUTO sensor fit."""
    n, f = clip_start, clip_end
    if cam_type == "orthographic":
        base = ortho_scale / 2.0
    else:
        base = n * (sensor_mm / 2.0) / lens_mm
    if width >= height:
        r, t = base, base * height / width
    else:
        r, t = base * width / height, base
    if cam_type == "orthographic":
        return [
            [1.0 / r, 0.0, 0.0, 0.0],
            [0.0, 1.0 / t, 0.0, 0.0],
            [0.0, 0.0, -2.0 / (f - n), -(f + n) / (f - n)],
            [0.0, 0.0, 0.0, 1.0],
        ]
    return [
        [n / r, 0.0, 0.0, 0.0],
        [0.0, n / t, 0.0, 0.0],
        [0.0, 0.0, -(f + n) / (f - n), -2.0 * f * n / (f - n)],
        [0.0, 0.0, -1.0, 0.0],
    ]


def standard_camera(
    name: str,
    front: Sequence[float],
    up: Sequence[float],
    center: Sequence[float],
    radius: float,
    resolution: int,
) -> dict[str, object]:
    """Camera for a named view, framed on a bounding sphere.

    Orthographic views share one ortho scale so the plates register at the
    same scale; the perspective view uses a 50 mm lens.
    """
    radius = max(radius, 1e-4)
    direction, cam_up = view_direction(name, front, up)
    if name in ORTHO_VIEWS:
        distance = 4.0 * radius
        cam = {
            "type": "orthographic",
            "lens_mm": STANDARD_LENS_MM,
            "ortho_scale": 2.3 * radius,
            "clip_start": 0.5 * radius,
            "clip_end": 8.0 * radius,
        }
    else:
        half_fov = math.atan((SENSOR_WIDTH_MM / 2.0) / STANDARD_LENS_MM)
        distance = 1.12 * radius / math.sin(half_fov)
        cam = {
            "type": "perspective",
            "lens_mm": STANDARD_LENS_MM,
            "ortho_scale": None,
            "clip_start": max(distance - 2.0 * radius, distance * 0.05),
            "clip_end": distance + 4.0 * radius,
        }
    location = add(center, scale(direction, distance))
    cam.update(
        {
            "name": name,
            "location": list(location),
            "target": list(center),
            "up": list(cam_up),
            "matrix_world": look_at(location, center, cam_up),
            "width": resolution,
            "height": resolution,
            "sensor_mm": SENSOR_WIDTH_MM,
        }
    )
    return cam


def custom_camera(spec: dict[str, object], frame_up: Sequence[float], radius: float, resolution: int) -> dict:
    location = tuple(spec["location"])  # type: ignore[arg-type]
    target = tuple(spec["target"])  # type: ignore[arg-type]
    up = tuple(spec.get("up") or frame_up)  # type: ignore[arg-type]
    distance = length(sub(location, target))
    ortho = spec.get("type") == "orthographic"
    return {
        "name": spec["name"],
        "type": "orthographic" if ortho else "perspective",
        "lens_mm": float(spec.get("lens_mm") or STANDARD_LENS_MM),  # type: ignore[arg-type]
        "ortho_scale": float(spec.get("ortho_scale") or 2.3 * radius) if ortho else None,  # type: ignore[arg-type]
        "clip_start": max(min(distance * 0.01, radius * 0.05), 1e-4),
        "clip_end": distance + 6.0 * max(radius, 1e-4),
        "location": list(location),
        "target": list(target),
        "up": list(up),
        "matrix_world": look_at(location, target, up),
        "width": resolution,
        "height": resolution,
        "sensor_mm": SENSOR_WIDTH_MM,
    }


def matmul(a: Mat4, b: Mat4) -> Mat4:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def transform4(m: Mat4, v: Sequence[float]) -> tuple[float, float, float, float]:
    return tuple(sum(m[i][k] * v[k] for k in range(4)) for i in range(4))  # type: ignore[return-value]


def inverse(m: Mat4) -> Mat4:
    """Gauss-Jordan inverse of a 4x4 matrix."""
    a = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(4)] for i, row in enumerate(m)]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-15:
            raise ValueError("matrix is singular")
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [x / p for x in a[col]]
        for r in range(4):
            if r != col:
                factor = a[r][col]
                if factor:
                    a[r] = [x - factor * y for x, y in zip(a[r], a[col], strict=True)]
    return [row[4:] for row in a]


def image_ray(camera: dict, u: float, v: float) -> dict[str, object]:
    """World-space ray through normalised image coords (top-left origin)."""
    view_proj = matmul(camera["projection"], inverse(camera["matrix_world"]))
    inv = inverse(view_proj)
    x, y = 2.0 * u - 1.0, 1.0 - 2.0 * v
    near = transform4(inv, (x, y, -1.0, 1.0))
    far = transform4(inv, (x, y, 1.0, 1.0))
    p0 = (near[0] / near[3], near[1] / near[3], near[2] / near[3])
    p1 = (far[0] / far[3], far[1] / far[3], far[2] / far[3])
    return {"origin": list(p0), "direction": list(normalize(sub(p1, p0))), "max_distance": length(sub(p1, p0))}


def project_point(camera: dict, point: Sequence[float]) -> tuple[float, float, float]:
    """Project a world point to normalised image coords (u, v) and NDC depth."""
    view_proj = matmul(camera["projection"], inverse(camera["matrix_world"]))
    c = transform4(view_proj, (point[0], point[1], point[2], 1.0))
    x, y, z = c[0] / c[3], c[1] / c[3], c[2] / c[3]
    return (x + 1.0) / 2.0, (1.0 - y) / 2.0, z
