"""SENTINEL VISION solution presentation: one content spec -> PPTX + HTML (for PDF)."""
import html, os, sys
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Inches, Pt, Emu

DOC = sys.argv[1]
SHOT = os.path.join(DOC, "screenshots")
DIAG = os.path.join(DOC, "diagrams")

NAVY, PANEL, BORDER = "0B1628", "12223A", "24476F"
CYAN, TEAL, TEXT, MUTED, ORANGE, RED, AMBER, VIOLET = "22D3EE", "2DD4BF", "E6EDF6", "93A4BC", "F97316", "EF4444", "F59E0B", "A78BFA"
TAGS = {"CURRENT": TEAL, "DEMONSTRATED": CYAN, "FUTURE": VIOLET, "LIMITATION": ORANGE}

# ---------------- content ----------------
SLIDES = [
    {"kind": "cover"},
    {"kind": "bullets", "kicker": "PROBLEM", "title": "The problem control rooms face today",
     "items": [("Fragmented CCTV", "Cameras sit behind different vendors, recorders and VMS products, each with its own screen."),
               ("Manual monitoring burden", "Operators watch many feeds at once; attention does not grow with camera count."),
               ("Delayed identification", "A watch-listed vehicle or a restricted-area intrusion is often found after the fact."),
               ("Hard investigations", "Tracing a vehicle means scrubbing footage camera by camera and collecting evidence by hand."),
               ("Camera health", "Streams drop, credentials fail and bandwidth varies, often without anyone noticing.")]},
    {"kind": "tiles", "kicker": "SOLUTION", "title": "SENTINEL VISION in one sentence",
     "lead": "One web platform that connects cameras from any source, watches them with AI, raises alerts when rules are broken, and hands operators verifiable evidence and a map to act on.",
     "tiles": [("Connect", "One camera registry for webcam, video, RTSP and the Sentinel Grid", CYAN),
               ("Detect", "People, vehicles and number plates, per camera", TEAL),
               ("Decide", "Zones, watchlists and loitering rules raise alerts", ORANGE),
               ("Respond", "Alerts grow into correlated incidents", RED),
               ("Prove", "Hashed snapshots, clips and incident evidence packages", CYAN),
               ("Stay up", "Self-Heal reconnects cameras and reports what failed", AMBER)]},
    {"kind": "image", "kicker": "ARCHITECTURE", "title": "Solution architecture", "image": os.path.join(DIAG, "architecture_diagram.png"),
     "caption": "Four tiers: Next.js dashboard · FastAPI REST + WebSocket · per-camera AI pipeline · SQLite / PostgreSQL + evidence store. Everything shown is implemented."},
    {"kind": "split", "kicker": "CAMERA INTEGRATION", "title": "CCTV and camera integration", "image": os.path.join(SHOT, "04_camera_control.png"),
     "items": [("Central camera registry", "Every camera in one catalogue with location, status and AI switches.", "CURRENT"),
               ("Camera adapters", "Webcam, uploaded video, RTSP (TCP or UDP), mock VMS behind one interface.", "CURRENT"),
               ("Sentinel Grid", "Catalogue sync of 30 cameras, autoconnect, bulk control. Streaming is partial today (see slide 12).", "DEMONSTRATED"),
               ("VMS federation", "New VMS products join as adapters; ONVIF is an interface stub.", "FUTURE")]},
    {"kind": "split", "kicker": "AI VIDEO ANALYTICS", "title": "AI video analytics", "image": os.path.join(SHOT, "05_live_ai_vision.png"),
     "items": [("Ultralytics YOLO11s", "Person and vehicle detection at 960 px on one shared GPU model (successor to the earlier YOLOv8s baseline).", "CURRENT"),
               ("ByteTrack", "A tracker per camera, so every object keeps one ID across frames.", "CURRENT"),
               ("EasyOCR ANPR", "A plate detector finds the plate; EasyOCR reads it; reads are voted across frames.", "CURRENT"),
               ("Selective analytics", "Person, vehicle and ANPR switched per camera; AI slots rotate when hardware is short.", "CURRENT")]},
    {"kind": "flow", "kicker": "EVENT PROCESSING", "title": "Intelligent event processing",
     "steps": [("Detection", "YOLO11s finds people and vehicles", TEAL), ("Tracking", "ByteTrack keeps one ID per object", TEAL),
               ("OCR", "Plate read, voted across frames", TEAL), ("Rules", "Zones · watchlists · loitering · risk score", ORANGE),
               ("Alert", "Reasons, severity, pushed live", ORANGE), ("Incident", "CRITICAL alerts open or join one", RED)],
     "notes": ["A one-frame plate read is stored but goes to review; CRITICAL watchlist alerts need corroboration across frames.",
               "Cooldowns per camera, rule and track stop one object raising the same alert every frame.",
               "One vehicle crossing several cameras becomes one incident whose title and priority grow with it."]},
    {"kind": "split", "kicker": "GIS", "title": "GIS and camera intelligence", "image": os.path.join(SHOT, "02_camera_map.png"),
     "items": [("19 of 30 grid cameras mapped", "Positions looked up in OpenStreetMap from each camera's place name.", "CURRENT"),
               ("11 intentionally not mapped", "No reliable match, so they are not guessed; the map says so.", "LIMITATION"),
               ("Clustered markers + legend", "Cameras sharing a spot (three in Bilimora) form one marker; amber = degraded.", "CURRENT"),
               ("Vehicle journey map", "Sightings drawn on camera positions; GRID-cam06 verified at Timbavadi, Junagadh.", "CURRENT")]},
    {"kind": "split", "kicker": "ALERTS & EVIDENCE", "title": "Alerts, incidents and evidence", "image": os.path.join(SHOT, "11_evidence_detail.png"),
     "items": [("Alerts", "Reasons, severity and risk factors; acknowledge, escalate or dismiss.", "CURRENT"),
               ("Incidents", "Correlated across cameras, with notes and a summary.", "CURRENT"),
               ("Evidence", "Snapshot per alert, event clips, operator REC recordings; SHA-256 at capture, VERIFIED on access.", "DEMONSTRATED"),
               ("Evidence package", "Per incident, JSON or PDF, optional plate redaction; every access audited.", "CURRENT")]},
    {"kind": "split", "kicker": "RELIABILITY", "title": "Self-Heal and reliability", "image": os.path.join(SHOT, "16_self_heal_health.png"),
     "items": [("Camera health", "Each camera reports CONNECTED, PROCESSING, DEGRADED, RECONNECTING or AUTH_ERROR.", "CURRENT"),
               ("Reconnect + backoff", "Per-camera backoff; a grid-wide breaker pauses 5 to 60 min and probes with one camera first.", "CURRENT"),
               ("Database retry", "Locked writes are rolled back, reapplied and logged as Self-Heal events.", "CURRENT"),
               ("Failure isolation", "One camera crashing never stops the others; the dashboard WebSocket reconnects itself.", "CURRENT")]},
    {"kind": "split", "kicker": "SECURITY", "title": "Security by design", "image": os.path.join(SHOT, "18_audit.png"),
     "items": [("Authentication", "bcrypt password hashes, JWT sessions, login rate limiting.", "CURRENT"),
               ("RBAC", "Five roles checked on every API route: Administrator, Control Room Operator, Investigator, Supervisor, Auditor.", "CURRENT"),
               ("Resource tokens", "Short-lived signed tokens for live streams (1 h) and evidence files (5 min).", "CURRENT"),
               ("Audit trail", "Hash-chained audit log; a verify endpoint detects any edited or deleted row.", "CURRENT")]},
    {"kind": "demo", "kicker": "CURRENT DEMONSTRATION", "title": "What works today",
     "shots": [(os.path.join(SHOT, "01_dashboard.png"), "Command centre"), (os.path.join(SHOT, "12_vehicle_journey.png"), "Vehicle journey"),
               (os.path.join(SHOT, "07_alert_detail.png"), "Alert detail"), (os.path.join(SHOT, "09_incident_detail.png"), "Incident")],
     "status": [("Demonstrated", "Login, dashboard, camera registry and map, live AI detection and tracking, ANPR, zone alerts, incidents, evidence with integrity check, Self-Heal screens.", CYAN),
                ("Known limits", "Grid streaming is partial: about 29 cameras degraded and about 3.4 Mbit/s observed recently. SQLite is single-writer; a login-contention fix is tested but not yet re-verified with 30 live grid streams.", ORANGE)]},
    {"kind": "twocol", "kicker": "SCALE", "title": "From today to larger deployments",
     "left": ("Current implementation", TEAL, ["Single backend process, one asyncio task per camera", "SQLite (WAL) for the demo", "PostgreSQL 16 + PostGIS in Docker Compose, built and migrated by CI", "Valkey for shared cooldowns and rate limits (optional)", "Tested: 1101 backend tests, 23 frontend unit tests, 10 browser tests"]),
     "right": ("Planned / future scale", VIOLET, ["PostgreSQL as the default database, load-tested with a live fleet", "Separate AI worker processes / nodes per camera group", "Event streaming between ingestion, analytics and alerting", "Edge AI at camera clusters sending events, not full video", "More VMS adapters (ONVIF, vendor SDKs); container orchestration"])},
    {"kind": "twocol", "kicker": "IMPACT", "title": "Expected benefits and how to measure them",
     "left": ("Expected benefits", CYAN, ["Operators work from alerts instead of watching every feed", "Faster identification of watch-listed vehicles and zone intrusions", "Investigations start from a vehicle journey and an evidence package", "Camera failures are visible and mostly self-recovering", "Every action and evidence access is accountable"]),
     "right": ("Evaluation metrics for a pilot", AMBER, ["Time from event to operator alert", "False alerts per camera-hour", "Plate read rate and plate accuracy on the pilot's own cameras", "Camera uptime and time to recover", "Time to assemble an incident's evidence"])},
    {"kind": "closing", "kicker": "CONCLUSION", "title": "SENTINEL VISION",
     "lead": "A working, tested CCTV intelligence platform: cameras from mixed sources, AI detection, tracking and plate reading, rule-based alerts and incidents, verifiable evidence, a camera map and Self-Heal, secured with RBAC and a tamper-evident audit trail.",
     "points": ["Built and demonstrated on real footage from a 30-camera grid", "Honest about limits: partial grid streaming, SQLite demo database, 19 of 30 cameras mapped", "Clear next step: a pilot on PostgreSQL with separate AI workers"]},
]

# ---------------- PPTX ----------------
prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
BLANK = prs.slide_layouts[6]


def rgb(h):
    return RGBColor.from_string(h)


def rect(slide, x, y, w, h, fill=PANEL, line=BORDER, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid(); s.fill.fore_color.rgb = rgb(fill)
    if line:
        s.line.color.rgb = rgb(line); s.line.width = Pt(1)
    else:
        s.line.fill.background()
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = 0.06
    s.shadow.inherit = False
    return s


def text(slide, x, y, w, h, runs, size=14, color=TEXT, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.1):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.02); tf.margin_top = tf.margin_bottom = Inches(0.02)
    tf.vertical_anchor = anchor
    paras = runs if isinstance(runs, list) else [runs]
    for i, para in enumerate(paras):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align; p.line_spacing = spacing
        parts = para if isinstance(para, list) else [(para, {})]
        for t, st in parts:
            r = p.add_run(); r.text = t
            r.font.size = Pt(st.get("size", size)); r.font.bold = st.get("bold", bold)
            r.font.color.rgb = rgb(st.get("color", color)); r.font.name = "Segoe UI"
        if i:
            p.space_before = Pt(st.get("before", 6) if parts else 6)
    return tb


def bg(slide):
    rect(slide, 0, 0, 13.333, 7.5, NAVY, None, MSO_SHAPE.RECTANGLE)


def header(slide, s, n):
    bg(slide)
    text(slide, 0.6, 0.38, 8, 0.3, s["kicker"], 11, CYAN, True)
    text(slide, 0.6, 0.66, 12, 0.7, s["title"], 28, TEXT, True)
    rect(slide, 0.6, 1.36, 0.9, 0.04, CYAN, None, MSO_SHAPE.RECTANGLE)
    text(slide, 0.6, 7.05, 8, 0.3, "SENTINEL VISION · Unified CCTV Intelligence & Real-Time Smart Policing Platform", 9, MUTED)
    text(slide, 11.9, 7.05, 0.9, 0.3, str(n), 9, MUTED, align=PP_ALIGN.RIGHT)


def tag(slide, x, y, label):
    c = TAGS[label]
    s = rect(slide, x, y, 1.25, 0.26, NAVY, c)
    tf = s.text_frame; tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
    r = p.add_run(); r.text = label; r.font.size = Pt(8); r.font.bold = True; r.font.color.rgb = rgb(c); r.font.name = "Segoe UI"


def picture(slide, path, x, y, w, h):
    rect(slide, x - 0.04, y - 0.04, w + 0.08, h + 0.08, PANEL, BORDER)
    # fit keeping aspect ratio
    from PIL import Image
    iw, ih = Image.open(path).size
    scale = min(w / iw, h / ih)
    pw, ph = iw * scale, ih * scale
    slide.shapes.add_picture(path, Inches(x + (w - pw) / 2), Inches(y + (h - ph) / 2), Inches(pw), Inches(ph))


for n, s in enumerate(SLIDES, start=1):
    sl = prs.slides.add_slide(BLANK)
    k = s["kind"]
    if k == "cover":
        bg(sl)
        rect(sl, 0, 0, 0.18, 7.5, CYAN, None, MSO_SHAPE.RECTANGLE)
        text(sl, 0.9, 1.4, 11, 0.4, "GOVERNMENT CHALLENGE · SOLUTION PRESENTATION", 13, CYAN, True)
        text(sl, 0.9, 2.0, 11.5, 1.2, "SENTINEL VISION", 60, TEXT, True)
        text(sl, 0.9, 3.25, 11, 0.8, "Unified CCTV Intelligence & Real-Time Smart Policing Platform", 24, "B8C6DA")
        rect(sl, 0.9, 4.25, 2.2, 0.05, TEAL, None, MSO_SHAPE.RECTANGLE)
        text(sl, 0.9, 4.55, 11, 0.9, ["Camera integration · AI video analytics · ANPR · Alerts & incidents · Evidence · GIS · Self-Heal",
                                        [("Every capability shown is implemented in the repository and labelled Current, Demonstrated or Future.", {"size": 13, "color": MUTED})]], 15, TEXT)
        text(sl, 0.9, 6.55, 11, 0.4, "github.com/RuudyLinux/SENTINEL-AI · September 2026", 11, MUTED)
        sl.notes_slide.notes_text_frame.text = "Introduce SENTINEL VISION and say up front that everything shown is labelled current, demonstrated or future."
        continue
    header(sl, s, n)
    if k == "bullets":
        for i, (h, d) in enumerate(s["items"]):
            y = 1.75 + i * 1.02
            rect(sl, 0.6, y, 12.1, 0.86)
            rect(sl, 0.6, y, 0.07, 0.86, ORANGE if i == 2 else CYAN, None, MSO_SHAPE.RECTANGLE)
            text(sl, 0.9, y + 0.1, 3.4, 0.7, h, 17, TEXT, True, anchor=MSO_ANCHOR.MIDDLE)
            text(sl, 4.3, y + 0.1, 8.2, 0.7, d, 14, MUTED, anchor=MSO_ANCHOR.MIDDLE)
    elif k == "tiles":
        text(sl, 0.6, 1.65, 12.1, 0.9, s["lead"], 18, TEXT)
        for i, (h, d, c) in enumerate(s["tiles"]):
            x, y = 0.6 + (i % 3) * 4.08, 2.85 + (i // 3) * 2.0
            rect(sl, x, y, 3.86, 1.78)
            rect(sl, x, y, 3.86, 0.07, c, None, MSO_SHAPE.RECTANGLE)
            text(sl, x + 0.25, y + 0.3, 3.4, 0.5, h, 20, c, True)
            text(sl, x + 0.25, y + 0.85, 3.4, 0.85, d, 13, MUTED)
    elif k == "image":
        picture(sl, s["image"], 0.9, 1.65, 11.5, 4.75)
        text(sl, 0.6, 6.5, 12.1, 0.45, s["caption"], 12, MUTED, align=PP_ALIGN.CENTER)
    elif k == "split":
        for i, (h, d, t) in enumerate(s["items"]):
            y = 1.7 + i * 1.3
            rect(sl, 0.6, y, 5.6, 1.16)
            text(sl, 0.8, y + 0.12, 3.9, 0.35, h, 15, TEXT, True)
            tag(sl, 4.8, y + 0.14, t)
            text(sl, 0.8, y + 0.48, 5.25, 0.66, d, 11.5, MUTED)
        picture(sl, s["image"], 6.55, 1.75, 6.15, 5.05)
    elif k == "flow":
        steps = s["steps"]
        w = 1.85
        for i, (h, d, c) in enumerate(steps):
            x = 0.6 + i * (w + 0.2)
            rect(sl, x, 1.9, w, 1.9)
            rect(sl, x, 1.9, w, 0.07, c, None, MSO_SHAPE.RECTANGLE)
            text(sl, x + 0.15, 2.15, w - 0.3, 0.45, h, 17, c, True, align=PP_ALIGN.CENTER)
            text(sl, x + 0.15, 2.7, w - 0.3, 1.0, d, 12, MUTED, align=PP_ALIGN.CENTER)
            if i < len(steps) - 1:
                a = sl.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x + w + 0.02), Inches(2.7), Inches(0.16), Inches(0.3))
                a.fill.solid(); a.fill.fore_color.rgb = rgb(CYAN); a.line.fill.background()
        for i, note in enumerate(s["notes"]):
            y = 4.3 + i * 0.78
            rect(sl, 0.6, y, 12.1, 0.64)
            text(sl, 0.85, y + 0.08, 11.7, 0.5, note, 13.5, TEXT, anchor=MSO_ANCHOR.MIDDLE)
    elif k == "demo":
        for i, (p, cap) in enumerate(s["shots"]):
            x, y = 0.6 + (i % 2) * 3.55, 1.7 + (i // 2) * 2.6
            picture(sl, p, x, y, 3.35, 1.9)
            text(sl, x, y + 1.98, 3.35, 0.3, cap, 11, MUTED, align=PP_ALIGN.CENTER)
        for i, (h, d, c) in enumerate(s["status"]):
            y = 1.7 + i * 2.6
            rect(sl, 7.9, y, 4.8, 2.3)
            rect(sl, 7.9, y, 0.07, 2.3, c, None, MSO_SHAPE.RECTANGLE)
            text(sl, 8.15, y + 0.15, 4.4, 0.4, h, 17, c, True)
            text(sl, 8.15, y + 0.6, 4.4, 1.65, d, 12.5, TEXT)
    elif k == "twocol":
        for j, (col, x) in enumerate(((s["left"], 0.6), (s["right"], 6.8))):
            h, c, items = col
            rect(sl, x, 1.7, 5.95, 5.15)
            rect(sl, x, 1.7, 5.95, 0.07, c, None, MSO_SHAPE.RECTANGLE)
            text(sl, x + 0.3, 1.95, 5.4, 0.45, h, 19, c, True)
            text(sl, x + 0.3, 2.55, 5.4, 4.2, [[("■  ", {"color": c, "size": 10}), (it, {})] for it in items], 14.5, TEXT, spacing=1.15)
    elif k == "closing":
        text(sl, 0.6, 1.7, 12.1, 1.4, s["lead"], 19, TEXT)
        for i, pt in enumerate(s["points"]):
            y = 3.55 + i * 0.95
            rect(sl, 0.6, y, 12.1, 0.78)
            rect(sl, 0.6, y, 0.07, 0.78, [TEAL, ORANGE, CYAN][i], None, MSO_SHAPE.RECTANGLE)
            text(sl, 0.9, y + 0.1, 11.6, 0.6, pt, 16, TEXT, anchor=MSO_ANCHOR.MIDDLE)
        text(sl, 0.6, 6.45, 12.1, 0.4, "Thank you · github.com/RuudyLinux/SENTINEL-AI", 14, CYAN, True)

prs.save(os.path.join(DOC, "01_Solution_Presentation.pptx"))

# ---------------- HTML (same content) for the PDF ----------------
def e(s):
    return html.escape(s)


def img(p):
    return os.path.relpath(p, os.path.join(DOC, "source")).replace("\\", "/")


def tag_html(t):
    return f'<span class="tag" style="color:#{TAGS[t]};border-color:#{TAGS[t]}">{t}</span>'


pages = []
for n, s in enumerate(SLIDES, start=1):
    k = s["kind"]
    if k == "cover":
        pages.append('<section class="slide cover"><div class="stripe"></div><div class="kick">GOVERNMENT CHALLENGE · SOLUTION PRESENTATION</div>'
                     '<h1>SENTINEL VISION</h1><div class="sub">Unified CCTV Intelligence &amp; Real-Time Smart Policing Platform</div><div class="bar"></div>'
                     '<p class="cl">Camera integration · AI video analytics · ANPR · Alerts &amp; incidents · Evidence · GIS · Self-Heal</p>'
                     '<p class="muted">Every capability shown is implemented in the repository and labelled Current, Demonstrated or Future.</p>'
                     '<div class="foot">github.com/RuudyLinux/SENTINEL-AI · September 2026</div></section>')
        continue
    body = ""
    if k == "bullets":
        body = "".join(f'<div class="row"><div class="acc" style="background:#{ORANGE if i == 2 else CYAN}"></div><b>{e(h)}</b><span>{e(d)}</span></div>' for i, (h, d) in enumerate(s["items"]))
    elif k == "tiles":
        body = f'<p class="lead">{e(s["lead"])}</p><div class="tiles">' + "".join(
            f'<div class="tile" style="border-top-color:#{c}"><b style="color:#{c}">{e(h)}</b><span>{e(d)}</span></div>' for h, d, c in s["tiles"]) + "</div>"
    elif k == "image":
        body = f'<div class="bigimg"><img src="{img(s["image"])}"></div><p class="cap">{e(s["caption"])}</p>'
    elif k == "split":
        body = '<div class="split"><div class="cards">' + "".join(
            f'<div class="card"><div class="ch"><b>{e(h)}</b>{tag_html(t)}</div><span>{e(d)}</span></div>' for h, d, t in s["items"]) + \
            f'</div><div class="shot"><img src="{img(s["image"])}"></div></div>'
    elif k == "flow":
        body = '<div class="flow">' + '<i class="arr">▶</i>'.join(
            f'<div class="step" style="border-top-color:#{c}"><b style="color:#{c}">{e(h)}</b><span>{e(d)}</span></div>' for h, d, c in s["steps"]) + "</div>" + \
            "".join(f'<div class="note">{e(x)}</div>' for x in s["notes"])
    elif k == "demo":
        body = '<div class="demo"><div class="shots">' + "".join(f'<figure><img src="{img(p)}"><figcaption>{e(c)}</figcaption></figure>' for p, c in s["shots"]) + \
               '</div><div class="stat">' + "".join(f'<div class="sbox" style="border-left-color:#{c}"><b style="color:#{c}">{e(h)}</b><span>{e(d)}</span></div>' for h, d, c in s["status"]) + "</div></div>"
    elif k == "twocol":
        body = '<div class="two">' + "".join(
            f'<div class="col" style="border-top-color:#{c}"><b style="color:#{c}">{e(h)}</b><ul>' + "".join(f'<li style="--c:#{c}">{e(i)}</li>' for i in items) + "</ul></div>"
            for h, c, items in (s["left"], s["right"])) + "</div>"
    elif k == "closing":
        body = f'<p class="lead big">{e(s["lead"])}</p>' + "".join(
            f'<div class="row"><div class="acc" style="background:#{[TEAL, ORANGE, CYAN][i]}"></div><span class="w">{e(p)}</span></div>' for i, p in enumerate(s["points"])) + \
            '<p class="thanks">Thank you · github.com/RuudyLinux/SENTINEL-AI</p>'
    pages.append(f'<section class="slide"><div class="kick">{e(s["kicker"])}</div><h2>{e(s["title"])}</h2><div class="rule"></div>{body}'
                 f'<div class="foot">SENTINEL VISION · Unified CCTV Intelligence &amp; Real-Time Smart Policing Platform</div><div class="num">{n}</div></section>')

CSS = """
@page { size: 13.333in 7.5in; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Segoe UI', Inter, Arial, sans-serif; background: #0B1628; color: #E6EDF6; }
.slide { width: 13.333in; height: 7.5in; position: relative; padding: .38in .6in; overflow: hidden; break-after: page; background: #0B1628; }
.kick { color: #22D3EE; font-size: 11pt; font-weight: 700; letter-spacing: .04em; }
h2 { font-size: 28pt; margin: .06in 0 0; font-weight: 700; }
.rule { width: .9in; height: .04in; background: #22D3EE; margin: .1in 0 .28in; }
.foot { position: absolute; left: .6in; bottom: .22in; font-size: 9pt; color: #93A4BC; }
.num { position: absolute; right: .55in; bottom: .22in; font-size: 9pt; color: #93A4BC; }
.row { display: flex; align-items: center; gap: .3in; background: #12223A; border: 1px solid #24476F; border-radius: 6px; height: .86in; margin-bottom: .16in; padding-right: .3in; position: relative; overflow: hidden; }
.row .acc { position: absolute; left: 0; top: 0; bottom: 0; width: .07in; }
.row b { width: 3.4in; padding-left: .3in; font-size: 17pt; flex: none; }
.row span { color: #93A4BC; font-size: 14pt; }
.row span.w { color: #E6EDF6; font-size: 16pt; padding-left: .3in; }
.lead { font-size: 18pt; margin: 0 0 .3in; line-height: 1.35; }
.lead.big { font-size: 19pt; margin-bottom: .4in; }
.tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: .22in; }
.tile { background: #12223A; border: 1px solid #24476F; border-top: .07in solid; border-radius: 6px; height: 1.78in; padding: .28in .25in; display: flex; flex-direction: column; gap: .12in; }
.tile b { font-size: 20pt; } .tile span { color: #93A4BC; font-size: 13pt; }
.bigimg { height: 4.75in; display: flex; justify-content: center; } .bigimg img { max-height: 100%; max-width: 100%; border: 1px solid #24476F; border-radius: 6px; }
.cap { text-align: center; color: #93A4BC; font-size: 12pt; margin-top: .15in; }
.split { display: grid; grid-template-columns: 5.6in 1fr; gap: .35in; }
.cards { display: flex; flex-direction: column; gap: .14in; }
.card { background: #12223A; border: 1px solid #24476F; border-radius: 6px; height: 1.16in; padding: .12in .2in; }
.ch { display: flex; justify-content: space-between; align-items: center; margin-bottom: .06in; } .ch b { font-size: 15pt; }
.card span { color: #93A4BC; font-size: 11.5pt; line-height: 1.3; display: block; }
.tag { font-size: 8pt; font-weight: 700; border: 1px solid; border-radius: 4px; padding: 1px 8px; background: #0B1628; }
.shot { display: flex; align-items: flex-start; } .shot img { width: 100%; border: 1px solid #24476F; border-radius: 6px; }
.flow { display: flex; align-items: center; gap: .04in; margin: .2in 0 .45in; }
.step { flex: 1; background: #12223A; border: 1px solid #24476F; border-top: .07in solid; border-radius: 6px; height: 1.9in; padding: .22in .15in; text-align: center; display: flex; flex-direction: column; gap: .15in; }
.step b { font-size: 17pt; } .step span { color: #93A4BC; font-size: 12pt; }
.arr { color: #22D3EE; font-style: normal; font-size: 12pt; }
.note { background: #12223A; border: 1px solid #24476F; border-radius: 6px; padding: .16in .25in; font-size: 13.5pt; margin-bottom: .14in; }
.demo { display: grid; grid-template-columns: 7.1in 1fr; gap: .2in; }
.shots { display: grid; grid-template-columns: 1fr 1fr; gap: .15in .2in; }
.shots figure { margin: 0; } .shots img { width: 100%; border: 1px solid #24476F; border-radius: 6px; }
.shots figcaption { text-align: center; color: #93A4BC; font-size: 11pt; margin-top: .04in; }
.stat { display: flex; flex-direction: column; gap: .3in; }
.sbox { background: #12223A; border: 1px solid #24476F; border-left: .07in solid; border-radius: 6px; padding: .18in .25in; min-height: 2.3in; display: flex; flex-direction: column; gap: .12in; }
.sbox b { font-size: 17pt; } .sbox span { font-size: 12.5pt; line-height: 1.4; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: .25in; }
.col { background: #12223A; border: 1px solid #24476F; border-top: .07in solid; border-radius: 6px; height: 5.15in; padding: .25in .3in; }
.col b { font-size: 19pt; } .col ul { list-style: none; padding: 0; margin: .25in 0 0; }
.col li { font-size: 14.5pt; margin-bottom: .2in; padding-left: .28in; position: relative; line-height: 1.3; }
.col li::before { content: ''; position: absolute; left: 0; top: .1in; width: .1in; height: .1in; background: var(--c); }
.thanks { color: #22D3EE; font-weight: 700; font-size: 14pt; margin-top: .35in; }
.cover { padding: 1.4in .9in; }
.cover .stripe { position: absolute; left: 0; top: 0; bottom: 0; width: .18in; background: #22D3EE; }
.cover .kick { font-size: 13pt; } .cover h1 { font-size: 60pt; margin: .2in 0 .1in; }
.cover .sub { font-size: 24pt; color: #B8C6DA; } .cover .bar { width: 2.2in; height: .05in; background: #2DD4BF; margin: .35in 0 .3in; }
.cover .cl { font-size: 15pt; margin: 0 0 .1in; } .muted { color: #93A4BC; font-size: 13pt; }
.cover .foot { left: .9in; bottom: .5in; font-size: 11pt; }
"""
open(os.path.join(DOC, "source", "presentation.html"), "w", encoding="utf-8").write(
    f'<!doctype html><html><head><meta charset="utf-8"><title>SENTINEL VISION – Solution Presentation</title><style>{CSS}</style></head><body>{"".join(pages)}</body></html>')
print("slides", len(SLIDES))
