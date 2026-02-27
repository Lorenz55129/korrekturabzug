"""Rule: Overprint detection via pikepdf (structured ExtGState parsing)."""

from __future__ import annotations

import logging
from typing import Any

import pikepdf

from app.models import OverprintCheckResult, RuleStatus

logger = logging.getLogger(__name__)


def check_overprint(
    pdf_path: str,
    cfg: dict[str, Any],
) -> OverprintCheckResult:
    """Detect overprint usage by inspecting ExtGState dictionaries.

    Uses pikepdf for structured access to /Resources /ExtGState.
    Returns WARN when overprint is active, PASS otherwise.
    """
    pages_with_overprint: list[int] = []

    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_idx, page in enumerate(pdf.pages):
                if _page_has_overprint(page):
                    pages_with_overprint.append(page_idx + 1)
    except Exception as exc:
        logger.warning("Overprint-Check fehlgeschlagen: %s", exc)
        return OverprintCheckResult(
            overprint_used=False,
            pages_with_overprint=[],
            status=RuleStatus.WARN,
            message=f"Überdrucken-Prüfung eingeschränkt: {exc}",
        )

    if pages_with_overprint:
        pages_str = ", ".join(str(p) for p in pages_with_overprint)
        return OverprintCheckResult(
            overprint_used=True,
            pages_with_overprint=pages_with_overprint,
            status=RuleStatus.WARN,
            message=(
                f"Überdrucken aktiv auf Seite(n): {pages_str} "
                "– manuelle Prüfung empfohlen"
            ),
        )

    return OverprintCheckResult(
        overprint_used=False,
        pages_with_overprint=[],
        status=RuleStatus.PASS,
        message="Kein Überdrucken erkannt.",
    )


def _page_has_overprint(page: pikepdf.Page) -> bool:
    """Check whether any ExtGState on *page* has OP or op set to true."""
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        ext_gstate = resources.get("/ExtGState")
        if ext_gstate is None:
            return False

        gs_dict = ext_gstate
        if hasattr(gs_dict, "resolve"):
            gs_dict = gs_dict.resolve()

        for _name, gs_obj in gs_dict.items():
            try:
                gs = gs_obj.resolve() if hasattr(gs_obj, "resolve") else gs_obj
                if not isinstance(gs, pikepdf.Dictionary):
                    continue
                # /OP = overprint for stroking, /op = overprint for non-stroking
                op_stroke = gs.get("/OP")
                op_fill = gs.get("/op")
                if _is_true(op_stroke) or _is_true(op_fill):
                    return True
            except Exception:
                continue

    except Exception as exc:
        logger.debug("ExtGState parsing failed for page: %s", exc)

    return False


def _is_true(val: Any) -> bool:
    """Check whether a pikepdf value is boolean True."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    # pikepdf represents booleans as pikepdf.Name or bool
    return str(val).lower() == "true"
