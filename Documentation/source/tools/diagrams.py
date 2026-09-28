"""Generates the SENTINEL VISION architecture and workflow diagrams as SVG."""
import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1]
BG, PANEL, PANEL2, BORDER = "#0B1628", "#12223A", "#0F1D33", "#24476F"
CYAN, TEAL, TEXT, MUTED, ORANGE, RED, AMBER, GREEN = "#22D3EE", "#2DD4BF", "#E6EDF6", "#93A4BC", "#F97316", "#EF4444", "#F59E0B", "#22C55E"
FONT = "Inter, 'Segoe UI', Arial, sans-serif"


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts = w, h, []
        self.parts.append(f'<rect width="{w}" height="{h}" fill="{BG}"/>')
        self.title = title

    def text(self, x, y, s, size=14, color=TEXT, weight=400, anchor="start", italic=False):
        style = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), accent=CYAN, fill=PANEL, title_size=15, dashed=False, center=True):
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{accent if dashed else BORDER}" stroke-width="1.4"{dash}/>')
        self.parts.append(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>')
        cx = x + w / 2 if center else x + 16
        anchor = "middle" if center else "start"
        n = len(lines)
        top = y + h / 2 - (n * 17) / 2 + (4 if n else 5)
        self.text(cx, top, title, title_size, TEXT, 600, anchor)
        for i, ln in enumerate(lines):
            self.text(cx, top + 19 + i * 17, ln, 12, MUTED, 400, anchor)

    def arrow(self, x1, y1, x2, y2, color=CYAN, label=None, dashed=False, lx=None, ly=None):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.8" marker-end="url(#ah-{color[1:]})"{dash}/>')
        if label:
            self.text(lx if lx is not None else (x1 + x2) / 2 + 8, ly if ly is not None else (y1 + y2) / 2 + 4, label, 11, MUTED)

    def path(self, d, color=CYAN, dashed=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" marker-end="url(#ah-{color[1:]})"{dash}/>')

    def band(self, x, y, w, h, label, color):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" stroke="{color}" stroke-opacity="0.45" stroke-width="1.2" stroke-dasharray="3 4"/>')
        self.text(x + 12, y + 18, label, 11, color, 700)

    def save(self, name):
        markers = "".join(
            f'<marker id="ah-{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            for c in (CYAN, TEAL, MUTED, ORANGE, RED, AMBER, GREEN)
        )
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
               f'<title>{escape(self.title)}</title><defs>{markers}</defs>' + "".join(self.parts) + "</svg>")
        open(f"{OUT}/{name}.svg", "w", encoding="utf-8").write(svg)


def workflow():
    s = Svg(1500, 2080, "SENTINEL VISION end-to-end workflow and integration")
    s.text(40, 52, "SENTINEL VISION", 26, TEXT, 700)
    s.text(40, 80, "End-to-end workflow and integration diagram  ·  as implemented in the repository (September 2026)", 14, MUTED)
    s.text(1460, 52, "Solid border = implemented   Dashed border = planned / future", 12, MUTED, 400, "end")

    X, W = 380, 620  # main column
    cx = X + W / 2
    y = 120
    # camera sources
    s.band(40, y, 1420, 150, "1  CAMERA SOURCES", CYAN)
    srcs = [("Webcam", "device index"), ("Video file", "uploaded, looped"), ("RTSP", "TCP / UDP transport"),
            ("Sentinel Grid", "30-camera catalogue"), ("Mock VMS", "synthetic feed"), ("Future VMS", "ONVIF stub / adapters")]
    bw = 212
    for i, (t, sub) in enumerate(srcs):
        bx = 60 + i * (bw + 16)
        s.box(bx, y + 34, bw, 90, t, [sub], accent=CYAN if i < 5 else MUTED, dashed=(i == 5))
    y += 150 + 36
    steps = [
        ("2  CAMERA REGISTRY / ADAPTER LAYER", "Camera registry + catalogue sync", ["CameraSource adapters (adapters.py, source.py)", "Grid supervisor: autoconnect, staggered connects"], CYAN),
        ("3  VIDEO INGESTION", "Latest-frame reader per camera", ["Dedicated reader thread; grab() without decode when idle", "Stream-relative source timestamps"], CYAN),
        ("4  FRAME PROCESSING", "Camera worker loop (worker.py)", ["Preview/MJPEG, clip ring buffer, AI slot per camera", "AI on/off per camera, rotation when slots are short"], CYAN),
        ("5  SELECTIVE AI ANALYTICS", "Ultralytics YOLO11s · ByteTrack · plate detector + EasyOCR", ["Person / vehicle detection (shared GPU model)", "Per-camera ByteTrack IDs · multi-frame plate voting"], TEAL),
        ("6  EVENT CORRELATION", "Vehicles, plate sightings, tracks", ["Cross-camera vehicle journey · appearance signatures (similarity only)"], TEAL),
        ("7  RULES ENGINE", "Restricted zones · watchlists · loitering · risk score", ["Cooldowns, confidence gates, corroboration before CRITICAL"], ORANGE),
        ("8  ALERT", "Alert with reasons, severity, risk factors", ["Pushed live over WebSocket (alert.created)"], ORANGE),
        ("9  INCIDENT", "CRITICAL alerts open or join an incident", ["Correlation window groups one vehicle across cameras"], RED),
        ("10  EVIDENCE", "Snapshot · event clip · REC recording · evidence record", ["SHA-256 at capture · integrity check · custody in audit log"], RED),
        ("11  OPERATOR DASHBOARD", "Next.js 16 command centre (34 screens)", ["Live cameras, AI vision, map, alerts, incidents, investigation"], CYAN),
    ]
    ys = []
    for label, title, lines, accent in steps:
        h = 96
        s.text(X, y + 14, label, 11, accent, 700)
        s.box(X, y + 22, W, h - 22, title, lines, accent=accent)
        ys.append((y + 22, y + h))
        y += h + 34
    # main arrows
    s.arrow(cx, 120 + 150, cx, ys[0][0] - 20)
    for (a, b), (c, d) in zip(ys, ys[1:]):
        s.arrow(cx, b, cx, c - 20)
    # investigation package at bottom
    s.text(X, y + 14, "12  INVESTIGATION / EVIDENCE PACKAGE", 11, TEAL, 700)
    s.box(X, y + 22, W, 74, "Investigation workspace · vehicle journey · incident evidence package", ["JSON or PDF, optional plate redaction · signed short-lived tokens"], accent=TEAL)
    s.arrow(cx, ys[-1][1], cx, y + 22)
    bottom_main = y + 96

    # right column: health / self-heal
    RX, RW = 1060, 400
    top = ys[1][0]
    s.band(RX - 16, top - 30, RW + 32, 560, "RELIABILITY  (runs alongside every stage)", AMBER)
    s.box(RX, top, RW, 80, "Camera health monitoring", ["grid_state per camera: CONNECTED, PROCESSING,", "DEGRADED, RECONNECTING, AUTH_ERROR"], accent=AMBER)
    s.box(RX, top + 120, RW, 80, "Self-Heal engine", ["classifies failures, records events, opens problems", "DB lock retry with reapply (Self-Heal visible)"], accent=AMBER)
    s.box(RX, top + 240, RW, 80, "Reconnect / recovery", ["per-camera backoff; grid-wide breaker pauses", "5 to 60 min, one probe camera before the rest"], accent=AMBER)
    s.box(RX, top + 360, RW, 80, "WebSocket live status", ["/ws: camera.status, camera.health,", "self_heal.recovery pushed to the dashboard"], accent=AMBER)
    for k in range(3):
        s.arrow(RX + RW / 2, top + 80 + k * 120, RX + RW / 2, top + 120 + k * 120, AMBER)
    s.path(f"M {X + W} {ys[1][0] + 37} L {RX} {top + 40}", AMBER, dashed=True)
    s.path(f"M {RX + RW/2 - 120} {top + 440} L {RX + RW/2 - 120} {ys[-1][0] + 37} L {X + W} {ys[-1][0] + 37}", AMBER)

    # left column: platform services
    LX, LW = 40, 300
    ltop = ys[4][0]
    s.band(LX - 16 + 16, ltop - 30, LW + 16, 470, "PLATFORM SERVICES", TEAL)
    s.box(LX + 8, ltop, LW - 8, 80, "Database persistence", ["SQLite (WAL) in the demo", "PostgreSQL + PostGIS in Docker Compose"], accent=TEAL)
    s.box(LX + 8, ltop + 110, LW - 8, 80, "REST API", ["FastAPI · ~98 endpoints", "cameras, alerts, incidents, evidence…"], accent=TEAL)
    s.box(LX + 8, ltop + 220, LW - 8, 80, "WebSocket /ws", ["detection, plate, alert, incident,", "camera and self-heal events"], accent=TEAL)
    s.box(LX + 8, ltop + 330, LW - 8, 80, "Evidence store", ["snapshots, clips, recordings on disk", "hash recorded in the database"], accent=TEAL)
    for k in range(4):
        s.path(f"M {X} {ltop + 40 + k * 110} L {LX + LW} {ltop + 40 + k * 110}", TEAL, dashed=True)

    # security band
    sy = bottom_main + 40
    s.band(40, sy, 1420, 120, "CROSS-CUTTING SECURITY  (every request, stream and file)", CYAN)
    sec = [("Authentication", "bcrypt passwords · JWT", CYAN), ("RBAC", "5 roles, per-route checks", CYAN),
           ("Audit logging", "hash-chained audit log", CYAN), ("Protected resources", "stream + evidence tokens", CYAN)]
    for i, (t, sub, c) in enumerate(sec):
        bx = 70 + i * 350
        s.box(bx, sy + 34, 300, 70, t, [sub], accent=c)
        if i:
            s.arrow(bx - 50, sy + 69, bx, sy + 69, CYAN)
    s.h = sy + 180
    s.parts[0] = f'<rect width="{s.w}" height="{s.h}" fill="{BG}"/>'
    s.text(40, s.h - 24, "Source: github.com/RuudyLinux/SENTINEL-AI (branch demo-readiness-phase2). Names match the code modules they describe.", 11, MUTED)
    s.save("workflow_integration_diagram")


def architecture():
    s = Svg(1500, 1000, "SENTINEL VISION high-level architecture")
    s.text(40, 52, "SENTINEL VISION  ·  High-level architecture", 24, TEXT, 700)
    s.text(40, 80, "Everything in the boxes is implemented in the repository. Future scale is listed separately at the bottom.", 13, MUTED)
    # tiers
    s.band(40, 110, 1420, 120, "PRESENTATION", CYAN)
    s.box(70, 140, 420, 74, "Next.js 16 · React 18 dashboard", ["34 operator screens · Leaflet + OpenStreetMap map"])
    s.box(530, 140, 420, 74, "Live video in the browser", ["MJPEG (annotated) · WHEP where the grid offers it"])
    s.box(990, 140, 440, 74, "Operator roles", ["Administrator, Control Room Operator, Investigator,", "Supervisor, Auditor"])

    s.band(40, 260, 1420, 130, "API LAYER  (FastAPI / Uvicorn)", TEAL)
    s.box(70, 292, 330, 80, "REST API", ["~98 endpoints, 19 routers", "JWT auth · RBAC per route"], accent=TEAL)
    s.box(430, 292, 330, 80, "WebSocket /ws", ["12 live event types", "alerts, detections, camera health"], accent=TEAL)
    s.box(790, 292, 300, 80, "Stream + evidence access", ["short-lived resource tokens"], accent=TEAL)
    s.box(1120, 292, 310, 80, "Metrics + audit", ["Prometheus /metrics", "hash-chained audit log"], accent=TEAL)

    s.band(40, 420, 1020, 300, "VIDEO & AI PIPELINE  (one asyncio task per camera)", ORANGE)
    s.box(70, 452, 300, 80, "Camera adapters", ["webcam · file · RTSP · grid · mock VMS"], accent=CYAN)
    s.box(400, 452, 300, 80, "Frame reader + worker", ["latest frame, preview, clip buffer"], accent=CYAN)
    s.box(730, 452, 300, 80, "Detection + tracking", ["YOLO11s (shared GPU) · ByteTrack"], accent=ORANGE)
    s.box(70, 560, 300, 80, "ANPR", ["plate detector · EasyOCR · voting"], accent=ORANGE)
    s.box(400, 560, 300, 80, "Correlation + rules", ["vehicles, tracks · zones, watchlists"], accent=ORANGE)
    s.box(730, 560, 300, 80, "Alerts · incidents · evidence", ["snapshots, clips, REC recordings"], accent=RED)
    s.arrow(370, 492, 400, 492, CYAN); s.arrow(700, 492, 730, 492, CYAN)
    s.path("M 880 532 L 880 546 L 220 546 L 220 560", ORANGE)
    s.arrow(370, 600, 400, 600, ORANGE); s.arrow(700, 600, 730, 600, ORANGE)
    s.text(70, 690, "AI capacity guard: slots per camera, rotation when short. Model locks serialise GPU/OCR calls.", 12, MUTED)

    s.band(1090, 420, 370, 300, "OPERATIONS", AMBER)
    s.box(1110, 452, 330, 70, "Grid supervisor", ["autoconnect · breaker · probe camera"], accent=AMBER)
    s.box(1110, 540, 330, 70, "Self-Heal engine", ["events · problems · recovery actions"], accent=AMBER)
    s.box(1110, 628, 330, 70, "Governance", ["retention · purge · privacy controls"], accent=AMBER)

    s.band(40, 750, 1420, 150, "DATA", TEAL)
    s.box(70, 782, 330, 90, "SQLite (WAL)", ["demo / development database", "Alembic migrations"], accent=TEAL)
    s.box(430, 782, 330, 90, "PostgreSQL 16 + PostGIS", ["Docker Compose deployment path", "CI builds and migrates it"], accent=TEAL)
    s.box(790, 782, 300, 90, "Evidence store", ["files on disk / volume", "SHA-256 in DB"], accent=TEAL)
    s.box(1120, 782, 310, 90, "Valkey (optional)", ["shared cooldowns, rate limits", "in-process when absent"], accent=TEAL)
    s.text(40, 950, "Future scale (not implemented): multi-node workers, edge AI, message bus, container orchestration.", 12, MUTED, italic=True)
    s.save("architecture_diagram")


workflow()
architecture()
print("ok")
