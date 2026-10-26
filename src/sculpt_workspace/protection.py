"""Compare candidate fingerprints against the protection baseline, and diff
agent objects between revisions."""

from __future__ import annotations

from typing import Any

COMPONENT_REASONS = {
    "topology": "mesh topology changed (vertices, edges, or faces added, removed, or reconnected)",
    "coordinates": "vertex coordinates changed",
    "attributes": "mesh attributes changed (UVs, colours, creases, or other layers)",
    "shape_keys": "shape keys changed",
    "vertex_groups": "vertex groups or weights changed",
    "transform": "object transform, parenting, or constraints changed",
    "modifiers": "modifier stack or modifier settings changed",
    "evaluated_geometry": "evaluated geometry changed (the final shaped surface differs)",
    "data": "object data settings changed",
    "data_name": "mesh data block was replaced or is now shared differently",
}

# How to get the same result without touching protected geometry.
COMPONENT_HINTS = {
    "missing": "Protected objects cannot be deleted, unlinked or renamed away. To hide part of the sculpture, "
    "cover it with a new object instead.",
    "type": "Do not convert protected objects. Duplicate with sculpt.duplicate(obj) and convert the copy.",
    "topology": "Do not edit protected meshes (bmesh, mesh.from_pydata, remesh). Duplicate with "
    "sculpt.duplicate(obj) and edit the copy, or build separate geometry.",
    "coordinates": "Do not move protected vertices. Duplicate with sculpt.duplicate(obj) and edit the copy.",
    "attributes": "Do not add or change UVs, colour layers or attributes on protected meshes; do it on a copy.",
    "shape_keys": "Do not add, remove or change shape keys on protected meshes; do it on a copy.",
    "vertex_groups": "Do not change vertex groups on protected objects; do it on a copy.",
    "transform": "Do not move, rotate, scale, parent or constrain protected objects. To attach your object, "
    "parent YOUR object to the protected one (my_obj.parent = protected), never the reverse.",
    "modifiers": "Do not add or change modifiers on protected objects. Use them read-only, e.g. as a "
    "sculpt.conform_to / sculpt.boolean target, or work on a copy.",
    "evaluated_geometry": "Something changed the protected object's final shape (modifier, driver, or a "
    "referenced object it depends on). Leave its inputs alone.",
    "data": "Do not change settings of protected curves, text or other data; do it on a copy.",
    "data_name": "Never share or swap a protected mesh's data. Copy with sculpt.duplicate(obj) (it copies the "
    "data), not obj.copy() alone, and do not assign obj.data on a protected object.",
    "unreliable": "This component cannot be compared reliably, so the edit cannot be proven safe. Avoid "
    "touching this object and anything it depends on.",
}


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a protection report.

    ``ok`` is true only when every protected object is present and every
    fingerprinted component is identical and reliably evaluated.
    """
    violations: list[dict[str, Any]] = []
    for oid, base in sorted(baseline.items()):
        cand = candidate.get(oid)
        if cand is None:
            violations.append(
                {
                    "object_id": oid,
                    "name": base.get("name"),
                    "reasons": ["protected object is missing or was deleted"],
                    "components": ["missing"],
                    "hints": [COMPONENT_HINTS["missing"]],
                }
            )
            continue
        reasons: list[str] = []
        failed: list[str] = []
        if cand.get("type") != base.get("type"):
            reasons.append(f"object type changed from {base.get('type')} to {cand.get('type')}")
            failed.append("type")
        for item in cand.get("unreliable", []):
            reasons.append(f"cannot be validated reliably: {item.get('component')}: {item.get('reason')}")
            failed.append("unreliable")
        base_c: dict[str, str] = base.get("components", {})
        cand_c: dict[str, str] = cand.get("components", {})
        for comp in sorted(set(base_c) | set(cand_c)):
            if base_c.get(comp) != cand_c.get(comp):
                if comp not in cand_c:
                    reasons.append(f"cannot be validated reliably: {comp} could not be evaluated")
                    failed.append("unreliable")
                else:
                    reasons.append(COMPONENT_REASONS.get(comp, f"{comp} changed"))
                    failed.append(comp)
        if reasons:
            components = list(dict.fromkeys(failed))
            violations.append(
                {
                    "object_id": oid,
                    "name": cand.get("name") or base.get("name"),
                    "reasons": reasons,
                    "components": components,
                    "hints": [COMPONENT_HINTS[c] for c in components if c in COMPONENT_HINTS],
                }
            )
    added = sorted(set(candidate) - set(baseline))
    return {
        "ok": not violations,
        "protected_objects": len(baseline),
        "violations": violations,
        "agent_objects": [{"object_id": oid, "name": candidate[oid].get("name")} for oid in added],
    }


def diff_revisions(
    base: dict[str, Any] | None, candidate: dict[str, Any], protected: set[str], base_ids: set[str] | None = None
) -> dict[str, Any]:
    """Which agent objects an edit added, modified or removed.

    `base` holds the base revision's fingerprints. Revisions saved before
    fingerprints were stored have none; then only `base_ids` (from the base
    inspection) is known and `modified` is None.
    """

    def entry(fps: dict[str, Any], oid: str) -> dict[str, Any]:
        return {"object_id": oid, "name": (fps.get(oid) or {}).get("name")}

    cand_agent = {oid for oid in candidate if oid not in protected}
    if base is not None:
        base_agent = {oid for oid in base if oid not in protected}
    else:
        base_agent = {oid for oid in base_ids or set() if oid not in protected}
    modified: list[dict[str, Any]] | None = None
    if base is not None:
        modified = [
            entry(candidate, oid)
            for oid in sorted(cand_agent & base_agent)
            if candidate[oid].get("components") != base[oid].get("components")
            or candidate[oid].get("type") != base[oid].get("type")
        ]
    return {
        "added": [entry(candidate, oid) for oid in sorted(cand_agent - base_agent)],
        "modified": modified,
        "removed": [entry(base or {}, oid) for oid in sorted(base_agent - cand_agent)],
    }


def changed_ids(changes: dict[str, Any] | None) -> list[str]:
    if not changes:
        return []
    return [e["object_id"] for key in ("added", "modified") for e in changes.get(key) or []]
