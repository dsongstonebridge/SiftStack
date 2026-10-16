"""OFFLINE. PDF report from analyze.py output. No network calls.

  python report.py --out DIR                                  market report
  python report.py --out DIR --deal-zips 74112 --deal-price 140000 --deal-label "1234 E 56th St"

Layout Jeff liked (Park Hill, 2026-10-16): summary, recommendations with reasons, how the ranking
works, method and limits, then tables at the end in landscape. RANK ONLY, never a score.
Standard Helvetica only, so it opens in any PDF reader or email preview.
"""
import argparse
import json
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate,
                                Paragraph, Table, TableStyle)

DARK, ACCENT, MUTED = colors.HexColor("#1a1a2e"), colors.HexColor("#0f3460"), colors.HexColor("#7f8c8d")
ALT, BORDER = colors.HexColor("#f8f9fa"), colors.HexColor("#dcdde1")
ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], textColor=DARK, fontSize=20, alignment=0, spaceAfter=4)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], textColor=MUTED, fontSize=9.5, spaceAfter=12)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=ACCENT, fontSize=13, spaceBefore=12, spaceAfter=5)
BODY = ParagraphStyle("B", parent=ss["Normal"], fontSize=10, leading=13.5, spaceAfter=6)
BUL = ParagraphStyle("BUL", parent=BODY, leftIndent=12, bulletIndent=2, spaceAfter=3)
CELL = ParagraphStyle("C", parent=ss["Normal"], fontSize=7.4, leading=9)
CELLB = ParagraphStyle("CB", parent=CELL, fontName="Helvetica-Bold")
HEAD = ParagraphStyle("H", parent=CELL, textColor=colors.white, fontName="Helvetica-Bold")


def esc(t):
    return str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def P(t, st=BODY):
    return Paragraph(t, st)


def bullets(items):
    return [Paragraph(t, BUL, bulletText="•") for t in items]


def money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return "$0 (unrecorded)" if v == 0 else f"${v:,.0f}"


def band(r):
    if not r.get("price_low"):
        return "no recorded prices"
    if r["price_low"] == r["price_high"]:
        return money(r["price_low"])
    return f"{money(r['price_low'])} to {money(r['price_high'])}"


def table(rows, widths):
    t = Table(rows, colWidths=widths, repeatRows=1)
    st = [("BACKGROUND", (0, 0), (-1, 0), ACCENT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("GRID", (0, 0), (-1, -1), 0.4, BORDER)]
    st += [("BACKGROUND", (0, i), (-1, i), ALT) for i in range(2, len(rows), 2)]
    t.setStyle(TableStyle(st))
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--deal-zips", default="")
    ap.add_argument("--deal-price", type=float, default=0)
    ap.add_argument("--deal-label", default="")
    a = ap.parse_args()
    out = Path(a.out)
    res = json.loads((out / "buyers.json").read_text(encoding="utf-8"))
    meta = res["meta"]
    area = ", ".join(c + " County" for c in meta["counties"])
    if a.deal_zips:
        zs = sorted(z.strip() for z in a.deal_zips.split(",") if z.strip())
        ranked = json.loads((out / f"deal_{'_'.join(zs)}_shortlist.json").read_text(encoding="utf-8"))
        title = f"Buyers for {a.deal_label or 'ZIP ' + ', '.join(zs)}"
        scope = (f"Business buyers who bought or hold single-family homes in ZIP {', '.join(zs)}"
                 + (f" and whose price range fits {money(a.deal_price)}" if a.deal_price else ""))
        fname = f"Buyers_{'_'.join(zs)}.pdf"
    else:
        ranked = res["ranked"]
        title = "Single-Family Business Buyers"
        scope = f"Every business that bought or sold a single-family home in {area}"
        fname = f"Buyers_{'_'.join(meta['counties'])}_{meta['built']}.pdf"
    buyers = [r for r in ranked if not r["seller_only"]]
    sellers = [r for r in ranked if r["seller_only"]]

    def who(r):
        return esc(" + ".join(r["names"]))

    story = [P(title, H1),
             P(f"{esc(scope)}, last {meta['days']} days ({meta['since']} to {meta['built']}). "
               "SiftMap data, read-only.", SUB),
             P("Summary", H2),
             *bullets([
                 f"{meta['rows']:,} single-family sales in the sweep area in the window; "
                 f"{meta['corporate_owned']:,} are now owned by a company and {meta['flip_exits']:,} were a "
                 "company selling a flipped house.",
                 f"<b>{len(buyers)} business buyers</b> ranked below, plus {len(sellers)} flippers who sold "
                 "but have no purchase recorded in the window.",
                 f"{len(res['excluded'])} names set aside (national builders, iBuyers and rental funds, "
                 "government, lenders, family trusts). They are listed at the end, not dropped.",
             ]),
             P("Who to call first", H2)]
    for r in buyers[:10]:
        story.append(P(f"<b>{r['rank'] if not a.deal_zips else buyers.index(r) + 1}. {who(r)}</b> "
                       f"({esc(r['mail'] or 'no mailing address on file')}). {esc('; '.join(r['why']))}. "
                       f"Pays {band(r)}. ZIPs bought: {esc(', '.join(r['zips_bought']) or 'n/a')}."))
    story += [
        P("How the ranking works", H2),
        P("An order, not a score. Buyers come before seller-only flippers. Then, in order: how many "
          "separate purchases (closings, not parcels), how many flipped houses they sold, whether they "
          "were active in the last 6 months, sheriff's-sale purchases, cash purchases, and how many "
          "properties they own overall. Companies sharing a real mailing address are ranked together as "
          "one operator."),
        P("Method and limits", H2),
        *bullets([
            "Read-only SiftMap searches and property lookups. Nothing was added, saved, tagged or changed "
            "in the DataSift account, and no paid service was used.",
            "SiftMap lags county recording by a few weeks. A $0 price means unrecorded, not free. Sales "
            "were not hand-checked against the county unless the report says so.",
            "Next: find the person behind each LLC (free SiftMap reverse-address and Oklahoma Secretary of "
            "State search first; paid lookups and skip tracing only after Jeff says yes).",
        ]),
        NextPageTemplate("land"), PageBreak(),
        P("Table 1. Ranked buyers", H2)]
    rows = [[P(h, HEAD) for h in ["Rank", "Buyer", "Why", "Price range", "ZIPs bought", "Total owned",
                                  "Mailing address"]]]
    for i, r in enumerate(buyers, 1):
        rows.append([P(str(i), CELLB), P(who(r), CELLB), P(esc("; ".join(r["why"])), CELL), P(band(r), CELL),
                     P(esc(", ".join(r["zips_bought"])), CELL), P(str(r["portfolio"] or "?"), CELL),
                     P(esc(r["mail"] or "not on file"), CELL)])
    story.append(table(rows, [0.4 * inch, 2.0 * inch, 2.9 * inch, 1.2 * inch, 1.0 * inch, 0.6 * inch, 1.9 * inch]))
    story += [PageBreak(), P("Table 2. Every purchase by these buyers", H2)]
    rows = [[P(h, HEAD) for h in ["Rank", "Buyer", "Date", "Price", "Cash", "Property", "Bought from"]]]
    for i, r in enumerate(buyers, 1):
        for x in r["purchases"]:
            rows.append([P(str(i), CELL), P(esc(x["buyer"]), CELL), P(x["date"], CELL), P(money(x["price"]), CELL),
                         P("Y" if x["cash"] else "", CELL), P(esc(x["address"]), CELL), P(esc(x["seller"]), CELL)])
    story.append(table(rows, [0.4 * inch, 2.0 * inch, 0.75 * inch, 0.9 * inch, 0.4 * inch, 2.6 * inch, 2.9 * inch]))
    if sellers:
        story += [PageBreak(), P("Table 3. Flippers who sold (no purchase recorded in the window)", H2)]
        rows = [[P(h, HEAD) for h in ["Company", "Houses sold", "Sold to", "Mailing address"]]]
        for r in sellers:
            rows.append([P(who(r), CELLB), P(str(r["exits"]), CELL),
                         P(esc("; ".join(sorted({str(x["buyer"]) for x in r["sales"]}))), CELL),
                         P(esc(r["mail"] or "not on file"), CELL)])
        story.append(table(rows, [2.6 * inch, 0.8 * inch, 4.3 * inch, 2.3 * inch]))
    story += [PageBreak(), P("Table 4. Set aside", H2)]
    rows = [[P(h, HEAD) for h in ["Name", "Why set aside", "Purchases", "Sales"]]]
    for e in res["excluded"]:
        rows.append([P(esc(e["name"]), CELL), P(esc(e["excluded_as"]), CELL), P(str(e["buys"]), CELL),
                     P(str(e["sells"]), CELL)])
    story.append(table(rows, [4.2 * inch, 3.8 * inch, 1.0 * inch, 1.0 * inch]))

    def footer(c, d, width=letter[0]):
        c.saveState()
        c.setFont("Helvetica", 7.5)
        c.setFillColor(MUTED)
        c.drawString(0.5 * inch, 0.35 * inch, f"{title} | SiftMap data pulled {meta['built']}")
        c.drawRightString(width - 0.5 * inch, 0.35 * inch, f"Page {d.page}")
        c.restoreState()

    def footer_land(c, d):
        c.setPageSize(landscape(letter))
        footer(c, d, landscape(letter)[0])

    path = out / fname
    doc = BaseDocTemplate(str(path), pagesize=letter, title=title)
    pw, ph = letter
    lw, lh = landscape(letter)
    doc.addPageTemplates([
        PageTemplate("port", [Frame(0.75 * inch, 0.6 * inch, pw - 1.5 * inch, ph - 1.3 * inch)], onPage=footer),
        PageTemplate("land", [Frame(0.5 * inch, 0.6 * inch, lw - 1.0 * inch, lh - 1.1 * inch)], onPage=footer_land),
    ])
    doc.build(story)
    print("wrote", path)


if __name__ == "__main__":
    main()
