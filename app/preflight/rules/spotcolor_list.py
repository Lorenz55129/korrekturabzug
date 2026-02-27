"""Rule: List all spot colours in the PDF and perform basic name checks."""

from __future__ import annotations

import logging
from typing import Any

import pikepdf

from app.models import RuleStatus, SpotColorEntry, SpotColorListResult

logger = logging.getLogger(__name__)


def check_spot_colors_list(
    pdf_path: str,
    cfg: dict[str, Any],
    allowed_spot_names: list[str] | None = None,
) -> SpotColorListResult:
    """Scan all pages for Separation colour spaces and list every spot colour.

    Checks:
    - Known process-colour names that appear as spot (e.g. "CMYK Cyan") → WARN
    - If *allowed_spot_names* is given: any spot NOT in the list → WARN
    - Otherwise → PASS (informational listing)
    """
    spots: dict[str, dict[str, Any]] = {}  # UPPER_NAME → {original_name, cmyk, pages}
    _MAX_XOBJECT_RECURSION = 5
    _MAX_XOBJECTS_TOTAL = 200

    def _xobj_key(name: str, xobj: Any) -> str:
        """Build a stable key for a resolved XObject to prevent revisiting."""
        try:
            og = xobj.objgen
            if og != (0, 0):
                return f"xref:{og[0]}:{og[1]}"
        except Exception:
            pass
        return f"name:{name}"

    def _collect_spots_from_resources(
        resources: Any, page_num: int, visited: set[str], depth: int,
    ) -> None:
        """Collect Separation spots from resources, recursing into Form XObjects."""
        if depth > _MAX_XOBJECT_RECURSION:
            return

        # 1) Scan /ColorSpace
        cs_dict = resources.get("/ColorSpace") if hasattr(resources, "get") else None
        if cs_dict is not None:
            if hasattr(cs_dict, "items"):
                for _alias, cs_obj in cs_dict.items():
                    try:
                        if isinstance(cs_obj, pikepdf.Array):
                            cs = cs_obj
                        elif hasattr(cs_obj, "resolve"):
                            cs = cs_obj.resolve()
                        else:
                            cs = cs_obj
                        if not isinstance(cs, pikepdf.Array) or len(cs) < 2:
                            continue
                        if str(cs[0]) != "/Separation":
                            continue
                        spot_name = str(cs[1]).lstrip("/")
                        upper = spot_name.upper()

                        if upper not in spots:
                            cmyk = _try_extract_cmyk(cs)
                            spots[upper] = {
                                "original_name": spot_name,
                                "cmyk": cmyk,
                                "pages": set(),
                            }
                        spots[upper]["pages"].add(page_num)
                    except Exception as exc:
                        logger.debug("Could not parse ColorSpace entry: %s", exc)

        # 2) Recurse into Form XObjects
        xobject_dict = resources.get("/XObject") if hasattr(resources, "get") else None
        if xobject_dict is None:
            return
        if not hasattr(xobject_dict, "items"):
            return
        for xobj_name, xobj_ref in xobject_dict.items():
            if len(visited) >= _MAX_XOBJECTS_TOTAL:
                return
            try:
                xobj = xobj_ref.resolve() if hasattr(xobj_ref, "resolve") else xobj_ref
                key = _xobj_key(str(xobj_name), xobj)
                if key in visited:
                    continue
                visited.add(key)
                # Only recurse into Form XObjects
                subtype = xobj.get("/Subtype") if hasattr(xobj, "get") else None
                if subtype is None or str(subtype) != "/Form":
                    continue
                sub_resources = xobj.get("/Resources")
                if sub_resources is not None:
                    _collect_spots_from_resources(
                        sub_resources, page_num, visited, depth + 1,
                    )
            except Exception as exc:
                logger.debug("Could not recurse into XObject: %s", exc)

    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                resources = page.get("/Resources")
                if resources is None:
                    continue
                visited: set[str] = set()
                _collect_spots_from_resources(resources, page_idx + 1, visited, depth=0)

    except Exception as exc:
        logger.warning("Could not scan spot colours: %s", exc)
        return SpotColorListResult(
            status=RuleStatus.WARN,
            messages=[f"Spotfarben-Scan fehlgeschlagen: {exc}"],
        )

    entries: list[SpotColorEntry] = []
    for upper, info in sorted(spots.items()):
        entries.append(SpotColorEntry(
            name=info["original_name"],
            cmyk=info["cmyk"],
            pages=sorted(info["pages"]),
        ))

    messages: list[str] = []
    status = RuleStatus.PASS

    # Basic name checks: warn if process-colour named spots found
    process_names = {"CYAN", "MAGENTA", "YELLOW", "BLACK", "KEY"}
    suspicious = [e for e in entries if e.name.upper() in process_names]
    if suspicious:
        status = RuleStatus.WARN
        names = ", ".join(e.name for e in suspicious)
        messages.append(f"Prozessfarben als Spotfarbe definiert: {names}")

    # Allowlist check (e.g. for product profiles)
    disallowed: list[str] = []
    if allowed_spot_names is not None and entries:
        allowed_upper = {n.strip().upper() for n in allowed_spot_names}
        for e in entries:
            if e.name.strip().upper() not in allowed_upper:
                disallowed.append(e.name)
        if disallowed:
            status = RuleStatus.WARN
            messages.append(
                f"Nicht erlaubte Spotfarben: {', '.join(disallowed)}"
            )

    if not entries:
        messages.append("Keine Spotfarben gefunden.")
    else:
        messages.append(f"{len(entries)} Spotfarbe(n) gefunden: "
                        + ", ".join(e.name for e in entries))

    return SpotColorListResult(
        spot_colors=entries,
        total_count=len(entries),
        disallowed_spots=disallowed,
        status=status,
        messages=messages,
    )


def _try_extract_cmyk(cs_array: pikepdf.Array) -> list[float] | None:
    """Attempt to extract CMYK values from the alternate colour space."""
    try:
        if len(cs_array) < 4:
            return None
        alt_cs = cs_array[2]
        if hasattr(alt_cs, "resolve"):
            alt_cs = alt_cs.resolve()
        alt_name = str(alt_cs) if not isinstance(alt_cs, pikepdf.Array) else str(alt_cs[0])
        if alt_name != "/DeviceCMYK":
            return None
        tint_fn = cs_array[3]
        if hasattr(tint_fn, "resolve"):
            tint_fn = tint_fn.resolve()
        if isinstance(tint_fn, pikepdf.Dictionary):
            c1 = tint_fn.get("/C1")
            if c1 is not None:
                vals = [float(v) for v in c1]
                if len(vals) == 4:
                    return [round(v * 100, 1) for v in vals]
        return None
    except Exception:
        return None
