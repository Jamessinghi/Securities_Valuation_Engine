"""Export the valuation summary to PDF (reportlab) or JPEG (Pillow)."""
from __future__ import annotations

import io

_STATUS_LABEL = {"ok": "OK", "partial": "PARTIAL", "na": "N/A"}


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return str(v)


def build_pdf(summary: dict, meta: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            leftMargin=1.2 * cm, rightMargin=1.2 * cm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=17, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7.2, leading=8.5)

    story = []
    story.append(Paragraph("Securities Valuation Engine", h1))
    story.append(Paragraph(
        f"{meta.get('market','')}:{meta.get('ticker','')} &nbsp;|&nbsp; valuation date {meta.get('date','')} "
        f"&nbsp;|&nbsp; {meta.get('current_label','')}", sub))
    iv = summary.get("intrinsic_value_per_share")
    ivc = summary.get("intrinsic_currency", "")
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"<b>Triangulated intrinsic value: {ivc} {_fmt(iv)} / share</b> "
        f"(median of {summary.get('n_intrinsic_families', 0)} independent valuation families; "
        f"{summary.get('n_intrinsic_models', 0)} models)", styles["Normal"]))
    c = summary.get("counts", {})
    story.append(Paragraph(f"Computed: {c.get('ok',0)} full · {c.get('partial',0)} partial · "
                           f"{c.get('na',0)} needs external data", sub))
    story.append(Spacer(1, 10))

    data = [["#", "Method", "Section", "Status", "Value", "Fair /sh"]]
    for r in summary.get("results", []):
        data.append([
            str(r["id"]),
            Paragraph(r["name"], cell),
            Paragraph(r["section"].split(". ", 1)[-1], cell),
            _STATUS_LABEL.get(r["status"], r["status"]),
            _fmt(r.get("value")),
            _fmt(r.get("intrinsic_ps")),
        ])
    tbl = Table(data, colWidths=[0.9 * cm, 6.2 * cm, 4.2 * cm, 1.8 * cm, 2.6 * cm, 2.1 * cm], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("FONTSIZE", (0, 1), (-1, -1), 7.2),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (3, 0), (5, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
    ]
    for i, r in enumerate(summary.get("results", []), start=1):
        col = {"ok": "#16a34a", "partial": "#d97706", "na": "#9ca3af"}.get(r["status"], "#000")
        style.append(("TEXTCOLOR", (3, i), (3, i), colors.HexColor(col)))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    story.append(Spacer(1, 8))
    story.append(Paragraph("Educational reference only. Not personalised investment advice.", sub))
    doc.build(story)
    return buf.getvalue()


def build_jpeg(summary: dict, meta: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    rows = summary.get("results", [])
    W, pad, rh, header = 1180, 30, 22, 150
    H = header + rh * (len(rows) + 1) + 60
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    def font(sz, bold=False):
        try:
            name = "Helvetica-Bold.ttf" if bold else "Helvetica.ttf"
            return ImageFont.truetype(name, sz)
        except Exception:
            return ImageFont.load_default()

    d.rectangle([0, 0, W, header], fill="#1f2937")
    d.text((pad, 24), "Securities Valuation Engine", font=font(30, True), fill="white")
    d.text((pad, 66), f"{meta.get('market','')}:{meta.get('ticker','')}  |  {meta.get('date','')}  |  "
                      f"{meta.get('current_label','')}", font=font(15), fill="#cbd5e1")
    iv = summary.get("intrinsic_value_per_share")
    d.text((pad, 96), f"Intrinsic value: {summary.get('intrinsic_currency','')} {_fmt(iv)} / share "
                      f"({summary.get('n_intrinsic_families', 0)} families; "
                      f"{summary.get('n_intrinsic_models', 0)} models)", font=font(18, True), fill="#4ade80")

    cols = [(pad, "#"), (pad + 45, "Method"), (pad + 520, "Section"),
            (pad + 850, "Status"), (pad + 970, "Value"), (pad + 1080, "Fair/sh")]
    y = header + 8
    d.rectangle([0, y, W, y + rh], fill="#e5e7eb")
    for x, t in cols:
        d.text((x, y + 4), t, font=font(13, True), fill="#111827")
    y += rh
    statcol = {"ok": "#16a34a", "partial": "#d97706", "na": "#9ca3af"}
    for i, r in enumerate(rows):
        if i % 2:
            d.rectangle([0, y, W, y + rh], fill="#f3f4f6")
        d.text((pad, y + 4), str(r["id"]), font=font(12), fill="#111827")
        d.text((pad + 45, y + 4), r["name"][:60], font=font(12), fill="#111827")
        d.text((pad + 520, y + 4), r["section"].split(". ", 1)[-1][:34], font=font(11), fill="#374151")
        d.text((pad + 850, y + 4), _STATUS_LABEL.get(r["status"], ""), font=font(12, True),
               fill=statcol.get(r["status"], "#000"))
        d.text((pad + 970, y + 4), _fmt(r.get("value"))[:12], font=font(12), fill="#111827")
        d.text((pad + 1080, y + 4), _fmt(r.get("intrinsic_ps"))[:10], font=font(12), fill="#111827")
        y += rh
    d.text((pad, y + 16), "Educational reference only. Not personalised investment advice.",
           font=font(12), fill="#6b7280")

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()
