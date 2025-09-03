# -*- coding: utf-8 -*-
"""
Draw.io → Normalisierte XML → FMEDA → Feedback (JSON)
Single-file Streamlit App

Start:
    streamlit run streamlit_app.py
"""

import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
import json
import hashlib
import os
import re
import base64
import zlib
from datetime import datetime

APP_TITLE = "Draw.io → Normalisierte XML → FMEDA → Feedback (JSON)"
DATA_DIR = "data"
COMMENTS_FILE = os.path.join(DATA_DIR, "comments.json")

# ---------------------------
# Utility
# ---------------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def file_sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def safe_text(v: str) -> str:
    if v is None:
        return ""
    # Remove basic HTML tags Draw.io sometimes embeds in 'value' but keep text
    return re.sub(r"<[^>]+>", "", v).strip()

def decode_drawio_diagram_text(diagram_text: str) -> str:
    """
    Draw.io (<diagram>...</diagram>) kann komprimiert (base64+deflate) sein.
    Dies dekodiert nach XML (<mxGraphModel>...</mxGraphModel>).
    - Wenn es bereits XML enthält, gib es direkt zurück.
    - Sonst: base64-decode + zlib.decompress(wbits=-15).
    """
    if not diagram_text:
        return ""
    if "<mxGraphModel" in diagram_text or "<mxfile" in diagram_text or "<root>" in diagram_text:
        return diagram_text
    try:
        raw = base64.b64decode(diagram_text)
        # Draw.io verwendet "raw deflate" (ohne zlib header), daher wbits=-15
        xml_bytes = zlib.decompress(raw, -15)
        return xml_bytes.decode("utf-8", errors="replace")
    except Exception:
        return diagram_text

# ---------------------------
# Draw.io Parsing
# ---------------------------

def parse_drawio_xml(xml_bytes: bytes):
    """
    Gibt (nodes, edges, dot, normalized_xml_str, meta) zurück.
    nodes: dict id -> {id, label, style, type_guess}
    edges: list of {id, source, target, label}
    dot: Graphviz DOT string (digraph)
    normalized_xml_str: unsere eigene, schlanke XML-Repräsentation
    meta: {diagram_hash, node_count, edge_count}
    """
    root = ET.fromstring(xml_bytes)

    # Fall A: <mxfile><diagram>...</diagram></mxfile>
    diagrams = root.findall(".//diagram")
    mx_root = None
    if diagrams:
        d = diagrams[0]
        mx_text = decode_drawio_diagram_text((d.text or "").strip())
        if mx_text:
            mx_root = ET.fromstring(mx_text)
    else:
        # Fall B: Datei ist bereits ein <mxGraphModel> oder enthält es irgendwo
        if root.tag == "mxGraphModel":
            mx_root = root
        else:
            found = root.findall(".//mxGraphModel")
            if found:
                mx_root = found[0]

    if mx_root is None:
        raise ValueError("Konnte kein <mxGraphModel> in der .drawio-Datei finden.")

    cell_root = mx_root.find(".//root")
    if cell_root is None:
        raise ValueError("Unerwartete Draw.io-Struktur: <root> unter <mxGraphModel> fehlt.")

    nodes = {}
    edges = []

    # Zellen einlesen
    for cell in cell_root.findall("mxCell"):
        cid = cell.get("id", "")
        value = cell.get("value", "")
        style = cell.get("style", "")
        is_vertex = cell.get("vertex") == "1"
        is_edge = cell.get("edge") == "1"
        source = cell.get("source")
        target = cell.get("target")

        if is_vertex:
            label = safe_text(value)
            nodes[cid] = {
                "id": cid,
                "label": label or f"Node_{cid}",
                "raw_value": value or "",
                "style": style or "",
            }
        elif is_edge:
            edges.append({
                "id": cid,
                "source": source,
                "target": target,
                "label": safe_text(value),
                "style": style or "",
            })

    # Typ-Heuristik
    for n in nodes.values():
        n["type_guess"] = guess_component_type(n["label"], n["style"])

    # DOT bauen
    dot = build_dot(nodes, edges)

    # Normalisierte XML erzeugen
    normalized_xml_str = build_normalized_xml(nodes, edges)

    meta = {
        "diagram_hash": file_sha256(xml_bytes),
        "node_count": len(nodes),
        "edge_count": len(edges),
    }
    return nodes, edges, dot, normalized_xml_str, meta

def guess_component_type(label: str, style: str) -> str:
    L = (label or "").lower()
    S = (style or "").lower()

    def contains_any(s, arr): return any(x in s for x in arr)

    if contains_any(L, ["and", "∧"]) or "shape=and" in S:
        return "AND Gate"
    if contains_any(L, ["or", "∨"]) or "shape=or" in S:
        return "OR Gate"
    if contains_any(L, ["watchdog", "wdt"]):
        return "Watchdog"
    if contains_any(L, ["sensor"]):
        return "Sensor"
    if contains_any(L, ["mcu", "microcontroller", "cpu", "uc"]):
        return "MCU"
    if contains_any(L, ["adc"]):
        return "ADC"
    if contains_any(L, ["ldo", "regulator"]):
        return "LDO/Regulator"
    if contains_any(L, ["comparator", "cmp"]):
        return "Comparator"
    if contains_any(L, ["opamp", "op-amp", "op amp", "amp"]):
        return "OpAmp"
    if contains_any(L, ["mosfet", "fet"]):
        return "MOSFET"
    if contains_any(L, ["connector", "conn", "stecker", "pin"]):
        return "Connector"
    if contains_any(L, ["can", "lin", "uart", "spi", "i2c", "ethernet"]):
        return "Interface"
    if contains_any(L, ["battery", "charger", "buck", "boost", "power", "psu"]):
        return "Power"
    return "Function"

def build_dot(nodes, edges):
    # Einfache gerichtete Darstellung
    lines = ["digraph G {", "rankdir=LR;", "node [shape=box, fontsize=10];"]
    for n in nodes.values():
        label = f"{n['label']}\\n({n['type_guess']})" if n.get("type_guess") else n["label"]
        lines.append(f"\"{n['id']}\" [label=\"{label}\"];")
    for e in edges:
        lab = f" [label=\"{e['label']}\"]" if e.get("label") else ""
        lines.append(f"\"{e.get('source','')}\" -> \"{e.get('target','')}\"{lab};")
    lines.append("}")
    return "\n".join(lines)

def build_normalized_xml(nodes, edges) -> str:
    root = ET.Element("normalizedDiagram")
    nlist = ET.SubElement(root, "nodes")
    for n in nodes.values():
        el = ET.SubElement(nlist, "node")
        el.set("id", n["id"])
        el.set("label", n["label"])
        el.set("type", n.get("type_guess", ""))
    elist = ET.SubElement(root, "edges")
    for e in edges:
        el = ET.SubElement(elist, "edge")
        el.set("id", e["id"])
        el.set("source", e.get("source") or "")
        el.set("target", e.get("target") or "")
        if e.get("label"):
            el.set("label", e["label"])
    return ET.tostring(root, encoding="utf-8").decode("utf-8")

# ---------------------------
# FMEDA Heuristik
# ---------------------------

FMEDA_COLUMNS = [
    "row_id",
    "component_id",
    "component_label",
    "component_type",
    "failure_mode",
    "effect",
    "detection",
    "diagnostic_coverage",
    "failure_rate_FIT",
    "safety_relevance",
    "notes",
]

FMEDA_TEMPLATES = {
    "Sensor": ["Open circuit", "Short circuit", "Drift/Offset"],
    "Comparator": ["Stuck high", "Stuck low", "Offset drift"],
    "OpAmp": ["Output saturates high", "Output saturates low", "Gain drift"],
    "MCU": ["CPU hang", "I/O stuck", "Clock fail"],
    "ADC": ["Conversion freeze", "Code stuck", "Reference drift"],
    "MOSFET": ["Drain-source short", "Open circuit", "Gate oxide short"],
    "LDO/Regulator": ["Output overvoltage", "Output undervoltage", "Shutdown stuck"],
    "Connector": ["Pin open", "Short between pins"],
    "Interface": ["Bus stuck dominant", "Bus stuck recessive", "Frame loss"],
    "Power": ["No output", "Overvoltage", "Undervoltage"],
    "AND Gate": ["Logical fault"],
    "OR Gate":  ["Logical fault"],
    "Function": ["Failure to perform function"],
}

def has_watchdog(nodes):
    return any(n.get("type_guess") == "Watchdog" for n in nodes.values())

def infer_effect(component_label, outgoing_count):
    if outgoing_count > 0:
        return f"Propagates to {outgoing_count} downstream node(s)"
    return "Local effect only"

def infer_detection(n, global_watchdog):
    t = n.get("type_guess")
    if t == "MCU" and global_watchdog:
        return "Watchdog supervision"
    if t in ("ADC", "Comparator", "OpAmp"):
        return "Range/consistency checks"
    if t in ("Connector", "Sensor"):
        return "Plausibility / continuity check"
    return "TBD"

def build_fmeda(nodes, edges):
    out_deg = {n: 0 for n in nodes}
    for e in edges:
        if e.get("source") in out_deg:
            out_deg[e["source"]] += 1

    rows = []
    global_wd = has_watchdog(nodes)

    rid = 1
    for nid, n in nodes.items():
        ctype = n.get("type_guess", "Function")
        modes = FMEDA_TEMPLATES.get(ctype, FMEDA_TEMPLATES["Function"])
        for fm in modes:
            rows.append({
                "row_id": f"R{rid:04d}",
                "component_id": nid,
                "component_label": n["label"],
                "component_type": ctype,
                "failure_mode": fm,
                "effect": infer_effect(n["label"], out_deg.get(nid, 0)),
                "detection": infer_detection(n, global_wd),
                "diagnostic_coverage": "",
                "failure_rate_FIT": "",
                "safety_relevance": "TBD",
                "notes": "",
            })
            rid += 1
    df = pd.DataFrame(rows, columns=FMEDA_COLUMNS)
    return df

# ---------------------------
# Kommentare (JSON) speichern/lesen
# ---------------------------

def load_comments():
    ensure_dirs()
    if not os.path.exists(COMMENTS_FILE):
        return []
    try:
        with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_comments(comments_list):
    ensure_dirs()
    with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(comments_list, f, ensure_ascii=False, indent=2)

def add_comment_entry(existing, entry):
    existing.append(entry)
    save_comments(existing)

# ---------------------------
# Streamlit UI
# ---------------------------

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)
st.caption("V1 – Heuristische FMEDA-Erzeugung. Kommentare werden in data/comments.json persistiert.")

with st.sidebar:
    st.markdown("### Schritte")
    st.markdown("1. `.drawio` hochladen")
    st.markdown("2. Parsing & Graph (optional)")
    st.markdown("3. Normalisierte XML & FMEDA erzeugen")
    st.markdown("4. Kommentare zur FMEDA als JSON speichern")
    st.divider()
    st.markdown("**Hinweis:** Die FMEDA ist eine **Skelett-Tabelle**. Raten, DC und Relevanz sind Platzhalter für deine Regeln/Daten.")

uploaded = st.file_uploader("Draw.io Datei hochladen (.drawio oder .xml)", type=["drawio", "xml"])

if uploaded is None:
    st.info("Bitte eine `.drawio`/`.xml`-Datei hochladen. Die App unterstützt auch komprimierte Draw.io-Diagramme.")
    st.stop()

raw = uploaded.read()
diagram_hash = file_sha256(raw)

try:
    nodes, edges, dot, normalized_xml_str, meta = parse_drawio_xml(raw)
except Exception as e:
    st.error(f"Fehler beim Parsen: {e}")
    st.stop()

col_a, col_b, col_c = st.columns([2,2,1])

with col_a:
    st.subheader("Graph (DOT-Vorschau)")
    # Ohne deprecated use_container_width
    st.graphviz_chart(dot)

    st.download_button(
        "Normalisierte XML herunterladen",
        data=normalized_xml_str.encode("utf-8"),
        file_name="normalized_diagram.xml",
        mime="application/xml"
    )

with col_b:
    st.subheader("Meta")
    st.json(meta, expanded=False)
    st.subheader("Knoten (Auszug)")
    nd = pd.DataFrame([
        {"id": n["id"], "label": n["label"], "type": n.get("type_guess",""), "style": n.get("style","")}
        for n in nodes.values()
    ])
    st.dataframe(nd, width="stretch", hide_index=True)

with col_c:
    st.subheader("Kanten")
    ed = pd.DataFrame(edges)
    st.dataframe(ed, width="stretch", hide_index=True)

st.divider()

st.subheader("FMEDA (Skelett)")
fmeda_df = build_fmeda(nodes, edges)
st.dataframe(fmeda_df, width="stretch", hide_index=True)

fm_csv = fmeda_df.to_csv(index=False).encode("utf-8")
fm_json = fmeda_df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")

d1, d2 = st.columns(2)
with d1:
    st.download_button("FMEDA als CSV herunterladen", data=fm_csv, file_name="fmeda.csv", mime="text/csv")
with d2:
    st.download_button("FMEDA als JSON herunterladen", data=fm_json, file_name="fmeda.json", mime="application/json")

st.divider()

existing_comments = load_comments()

st.subheader("Feedback / Kommentare zur FMEDA")
st.caption("Kommentare werden in `data/comments.json` gespeichert und bei jedem neuen Eintrag aktualisiert.")

row_ids = fmeda_df["row_id"].tolist()
row_labels = fmeda_df[["row_id","component_label","failure_mode"]].apply(
    lambda r: f"{r.row_id} | {r.component_label} | {r.failure_mode}", axis=1
).tolist()
row_map = dict(zip(row_labels, row_ids))

with st.form("comment_form", border=True):
    sel_row = st.selectbox("FMEDA-Zeile", options=row_labels)
    sel_col = st.selectbox("Bezug (Spalte)", options=[c for c in FMEDA_COLUMNS if c not in ("row_id","component_id")])
    severity = st.selectbox("Wichtung", options=["minor", "moderate", "major", "critical"], index=0)
    comment_text = st.text_area("Kommentar", placeholder="Kurz, präzise, sachlich.", height=120)
    submitted = st.form_submit_button("Kommentar speichern")

if submitted:
    if not comment_text.strip():
        st.warning("Bitte einen Kommentartext eingeben.")
    else:
        rid = row_map[sel_row]
        row = fmeda_df.loc[fmeda_df["row_id"] == rid].iloc[0].to_dict()
        entry = {
            "timestamp": now_iso(),
            "diagram_hash": diagram_hash,
            "row_id": rid,
            "component_id": row["component_id"],
            "component_label": row["component_label"],
            "component_type": row["component_type"],
            "field": sel_col,
            "severity": severity,
            "comment": comment_text.strip(),
            "context": {
                "failure_mode": row["failure_mode"],
                "effect": row["effect"],
                "detection": row["detection"],
                "diagnostic_coverage": row["diagnostic_coverage"],
                "failure_rate_FIT": row["failure_rate_FIT"],
                "safety_relevance": row["safety_relevance"],
            }
        }
        add_comment_entry(existing_comments, entry)
        st.success("Kommentar gespeichert.")

existing_comments = load_comments()
st.subheader("Vorliegende Kommentare")
if existing_comments:
    st.dataframe(pd.DataFrame(existing_comments), width="stretch", hide_index=True)
    st.download_button(
        "Kommentare (JSON) herunterladen",
        data=json.dumps(existing_comments, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="comments.json",
        mime="application/json"
    )
else:
    st.info("Noch keine Kommentare gespeichert.")

st.divider()

st.subheader("DOT-Quelltext (optional)")
st.code(dot, language="dot")
