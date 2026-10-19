"""Push each briefing (and any alerts) to where buyers already work: Slack, Microsoft
Teams, and email. Every channel is optional and switched on by its own secret; nothing is
sent unless it's configured."""
from __future__ import annotations

import html
import logging
import re

from . import config, http

log = logging.getLogger(__name__)


def channels() -> list[str]:
    out = []
    if config.SLACK_WEBHOOK_URL:
        out.append("slack")
    if config.TEAMS_WEBHOOK_URL:
        out.append("teams")
    if config.RESEND_API_KEY and config.BRIEFING_EMAIL_TO:
        out.append("email")
    return out


def _summary(body_md: str) -> tuple[str, str]:
    lines = body_md.splitlines()
    headline = next((l[2:].strip() for l in lines if l.startswith("# ")), "Substrate briefing")
    bottom = next((re.sub(r"\*\*Bottom line:\*\*\s*", "", l).strip() for l in lines if l.startswith("**Bottom line")), "")
    return headline, bottom


def _timing_lines(outlook: dict | None, n: int = 4) -> list[str]:
    parts = (outlook or {}).get("parts") or {}
    ranked = sorted(parts.items(), key=lambda kv: -kv[1]["p_price_up"])
    return [f"{m}: {p['action']} ({p['p_price_up']:.0%} chance price rises)" for m, p in ranked[:n]]


def slack_payload(brief: dict, alerts: list[dict], outlook: dict | None) -> dict:
    headline, bottom = _summary(brief["body_md"])
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
              {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Substrate · {brief['edition']} edition"}]},
              {"type": "section", "text": {"type": "mrkdwn", "text": bottom[:2900] or "_No summary_"}}]
    if alerts:
        txt = "\n".join(f"• *{a['kind'].title()}:* {a['title']}" + (f" <{a['url']}|source>" if a.get("url") else "")
                        for a in alerts[:8])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Alerts*\n{txt}"[:2900]}})
    timing = _timing_lines(outlook)
    if timing:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Buy timing*\n" + "\n".join(f"• {t}" for t in timing)}})
    blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open Substrate"},
                                                     "url": config.SITE_URL}]})
    return {"text": f"{headline}: {bottom}"[:300], "blocks": blocks}


def teams_payload(brief: dict, alerts: list[dict], outlook: dict | None) -> dict:
    """Adaptive Card for a Teams 'Workflows' incoming webhook."""
    headline, bottom = _summary(brief["body_md"])
    body = [{"type": "TextBlock", "text": headline, "weight": "Bolder", "size": "Large", "wrap": True},
            {"type": "TextBlock", "text": f"Substrate · {brief['edition']} edition", "isSubtle": True, "spacing": "None"},
            {"type": "TextBlock", "text": bottom, "wrap": True}]
    if alerts:
        body.append({"type": "TextBlock", "text": "Alerts", "weight": "Bolder", "spacing": "Medium"})
        body += [{"type": "TextBlock", "text": f"- **{a['kind'].title()}:** {a['title']}", "wrap": True, "spacing": "Small"}
                 for a in alerts[:8]]
    timing = _timing_lines(outlook)
    if timing:
        body.append({"type": "TextBlock", "text": "Buy timing", "weight": "Bolder", "spacing": "Medium"})
        body += [{"type": "TextBlock", "text": f"- {t}", "wrap": True, "spacing": "Small"} for t in timing]
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": body, "actions": [{"type": "Action.OpenUrl", "title": "Open Substrate", "url": config.SITE_URL}]}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def md_to_html(md: str) -> str:
    """Just enough Markdown for the briefing format: headings, bullets, numbered lists, bold, links."""
    out, in_list = [], None
    for raw in md.splitlines():
        line = html.escape(raw)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', line)
        m_ul, m_ol = re.match(r"^\s*[-*]\s+(.*)", line), re.match(r"^\s*\d+\.\s+(.*)", line)
        tag = "ul" if m_ul else "ol" if m_ol else None
        if in_list and tag != in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if tag:
            if not in_list:
                out.append(f"<{tag}>")
                in_list = tag
            out.append(f"<li>{(m_ul or m_ol).group(1)}</li>")
        elif line.startswith("# "):
            out.append(f"<h1 style='font-size:20px'>{line[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2 style='font-size:14px;color:#0f766e;text-transform:uppercase'>{line[3:]}</h2>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append(f"</{in_list}>")
    return "\n".join(out)


def email_payload(brief: dict, alerts: list[dict], outlook: dict | None) -> dict:
    headline, _ = _summary(brief["body_md"])
    alert_html = ""
    if alerts:
        alert_html = "<h2 style='font-size:14px;color:#b91c1c'>ALERTS</h2><ul>" + "".join(
            f"<li><b>{html.escape(a['kind'].title())}:</b> {html.escape(a['title'])}</li>" for a in alerts[:8]) + "</ul>"
    body = (f"<div style='font-family:Arial,sans-serif;max-width:680px;color:#111827'>{alert_html}"
            f"{md_to_html(brief['body_md'])}<p><a href='{config.SITE_URL}'>Open Substrate</a></p></div>")
    return {"from": config.BRIEFING_EMAIL_FROM, "to": [x.strip() for x in config.BRIEFING_EMAIL_TO.split(",") if x.strip()],
            "subject": f"[Substrate {brief['edition']}] {headline}"[:200], "html": body}


def send(brief: dict, alerts: list[dict], outlook: dict | None) -> dict:
    sent = {}
    for ch in channels():
        try:
            if ch == "slack":
                http.post(config.SLACK_WEBHOOK_URL, json=slack_payload(brief, alerts, outlook))
            elif ch == "teams":
                http.post(config.TEAMS_WEBHOOK_URL, json=teams_payload(brief, alerts, outlook))
            elif ch == "email":
                http.post("https://api.resend.com/emails", json=email_payload(brief, alerts, outlook),
                          headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"})
            sent[ch] = "ok"
        except Exception as e:      # one broken channel must never fail the pipeline
            log.warning("delivery to %s failed: %s", ch, e)
            sent[ch] = f"failed: {e.__class__.__name__}"
    return sent
