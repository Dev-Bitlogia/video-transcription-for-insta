"""One-shot extractor for GitHub Actions + n8n. MULTI-PLATFORM v7.
Usage: python cli_extract.py --url https://www.instagram.com/p/DfHXgY2CY2b/
Also works with TikTok and Facebook public video URLs - platform is detected from the URL.
Keys from env: APIFY_TOKEN, ASSEMBLYAI_API_KEY, OPENAI_API_KEY
Output: results/<video-id>.json and results/<video-id>.pdf
"""
import argparse
import json
import os
import re
from pathlib import Path

from extractor import run_extraction, extract_shortcode


def clean(text):
    """ReportLab Helvetica cannot print emoji / non-Latin. Keep it safe."""
    if not text:
        return ""
    text = str(text)
    # Remove emoji and non-Latin characters, keep basic punctuation
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return text.strip()


def build_pdf(result, pdf_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=4)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9, leading=12, textColor=colors.HexColor("#555555"))

    meta = result.get("meta", {})
    transcript = result.get("transcript", {})
    visual = result.get("visual", {})
    viral = result.get("viral", {})

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, topMargin=18*mm, bottomMargin=18*mm, leftMargin=18*mm, rightMargin=18*mm)
    story = []
    story.append(Paragraph("Video Script Extractor - Video Report", h1))
    story.append(Paragraph(clean(result.get("reel_url", "")), small))
    story.append(Paragraph(f"Platform: {clean(result.get('platform', meta.get('platform','-')))}  |  Video ID: {clean(result.get('shortcode',''))}  |  Created: {clean(result.get('created_at',''))}", small))
    story.append(Spacer(1, 4*mm))

    # Metrics - Apify returns views/likes/comments/owner, not videoPlayCount/likesCount
    views = meta.get("views") or meta.get("videoPlayCount") or meta.get("videoViewCount") or "-"
    likes = meta.get("likes") or meta.get("likesCount") or "-"
    comments = meta.get("comments")
    if comments is None:
        comments = meta.get("commentsCount", "-")
    owner = meta.get("owner") or meta.get("ownerUsername") or "-"
    music = meta.get("music") or "-"
    story.append(Paragraph(f"Views: {views}  |  Likes: {likes}  |  Comments: {comments}", body))
    story.append(Paragraph(f"Account: {clean(owner)}  |  Music: {clean(music)}", body))

    story.append(Paragraph("Spoken script", h2))
    story.append(Paragraph(clean(transcript.get("text", "")), body))
    if transcript.get("is_song"):
        story.append(Paragraph("Note: audio is only a song. Lyrics are not shown.", small))

    story.append(Paragraph("On-screen text", h2))
    ost = visual.get("on_screen_text") or []
    if ost and isinstance(ost[0], dict):
        ost_text = ", ".join(str(x.get("text", "")) for x in ost if x.get("text"))
    else:
        ost_text = ", ".join(str(x) for x in ost)
    story.append(Paragraph(clean(ost_text) if ost_text else "None - no text on screen", body))

    story.append(Paragraph("Visual hook (first 3 sec)", h2))
    story.append(Paragraph(clean(visual.get("visual_hook_0_3s", "-")), body))

    story.append(Paragraph("Hook - from this Reel", h2))
    story.append(Paragraph(f"Spoken: {clean(viral.get('hook_spoken','-'))}", body))
    story.append(Paragraph(f"Visual: {clean(viral.get('hook_visual','-'))}", body))
    story.append(Paragraph(f"Text: {clean(viral.get('hook_text','-'))}", body))

    tw = viral.get("trigger_words") or []
    if tw:
        story.append(Paragraph("Trigger words", h2))
        story.append(Paragraph(clean(", ".join(tw)), body))

    story.append(Paragraph("Verdict", h2))
    story.append(Paragraph(clean(viral.get("viral_verdict", "-")), body))

    why_viral = viral.get("why_viral") or []
    if why_viral:
        story.append(Paragraph("Why it went viral", h2))
        for item in why_viral:
            story.append(Paragraph(f"- {clean(item)}", body))

    why_not = viral.get("why_not_reaching") or []
    if why_not:
        story.append(Paragraph("Why it did NOT reach", h2))
        for item in why_not:
            story.append(Paragraph(f"- {clean(item)}", body))

    fixes = viral.get("what_to_fix") or []
    if fixes:
        story.append(Paragraph("What to fix", h2))
        for item in fixes:
            story.append(Paragraph(f"- {clean(item)}", body))

    # Replicable formula only if viral (why_viral is non-empty). Never show it for a non-viral Reel.
    formula = viral.get("replicable_formula", "")
    if formula and (viral.get("why_viral") or []):
        story.append(Paragraph("Replicable formula", h2))
        story.append(Paragraph(clean(formula), body))

    story.append(Paragraph("Caption", h2))
    story.append(Paragraph(clean(meta.get("caption", "-")), body))

    doc.build(story)
    return pdf_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="Video URL - Instagram, TikTok, or Facebook")
    p.add_argument("--out", default="results", help="Output folder")
    args = p.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    shortcode = extract_shortcode(args.url)
    print(f"Extracting {shortcode} ...")

    result = run_extraction(
        args.url,
        os.environ["APIFY_TOKEN"],
        os.environ["ASSEMBLYAI_API_KEY"],
        os.environ["OPENAI_API_KEY"],
        force=True,
    )
    json_path = outdir / f"{shortcode}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_path = outdir / f"{shortcode}.pdf"
    build_pdf(result, pdf_path)
    print(f"Done. JSON: {json_path}")
    print(f"Done. PDF: {pdf_path}")
    print(f"Verdict: {result.get('viral', {}).get('viral_verdict','-')}")
    print(f"Spoken script: {result.get('transcript', {}).get('text','')}")


if __name__ == "__main__":
    main()
