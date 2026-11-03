#!/usr/bin/env python3
"""Render an erwin .erwin model as one self-contained interactive HTML diagram.

The page shows every table and view with its columns, keys, relationships
(IE crow's-foot notation) and all the metadata the model holds: definitions,
logical names, business terms, subject areas, user-defined properties such
as Data Steward, valid-value lists, and keyword-based classification
suggestions. Click a table for the full detail panel.

Usage:
    erwin_diagram.py MODEL.erwin [--out FILE.html] [--title TITLE]
                     [--layout LAYOUT.json] [--save-layout FILE.json]
                     [--select TABLE]

No erwin install is needed: the model is read with the bundled parser
(erwin2postgres.py / erwinbin.py).
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL = os.path.dirname(HERE)
TEMPLATE = os.path.join(SKILL, "assets", "template.html")
LAYOUTS = os.path.join(SKILL, "assets", "layouts")

# Geometry shared with the template (W, HDR, ROW there).
BOX_W, HDR, ROW = 250, 46, 19
COL_STEP, GAP, MARGIN = 360, 50, 40


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def export(model, tmp):
    """Run the bundled parser; returns (model.json dict, glossary dir)."""
    meta, gloss = os.path.join(tmp, "meta"), os.path.join(tmp, "gloss")
    cmd = [sys.executable, "-I", os.path.join(HERE, "erwin2postgres.py"), model,
           "--out", os.path.join(tmp, "model.sql"), "--metadata", meta, "--glossary", gloss]
    res = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(res.stdout + res.stderr)
    if res.returncode != 0:
        sys.exit(f"parser failed on {model}")
    with open(os.path.join(meta, "model.json"), encoding="utf-8") as f:
        return json.load(f), gloss


def build_data(m, gloss, title):
    g = read_csv(os.path.join(gloss, "glossary.csv"))
    ta = read_csv(os.path.join(gloss, "term_assignments.csv"))
    cs = {r["Name"]: r for r in read_csv(os.path.join(gloss, "classification_suggestions.csv"))}
    cp = read_csv(os.path.join(gloss, "custom_properties.csv"))
    ex = read_csv(os.path.join(gloss, "extra_secondary_categories.csv"))
    vvu = read_csv(os.path.join(gloss, "reference_data_usage.csv"))

    roots = {r["Name"] for r in g if r["Artifact Type"] == "category" and not r["Category"]}
    terms = {r["Name"]: r for r in g if r["Artifact Type"] == "glossary_term"}
    term_of = {(r["table"], r["column"]): r["term"] for r in ta}
    props = defaultdict(dict)
    for r in cp:
        if not r["column"]:
            props[r["table"]][r["property"]] = r["value"]

    vv = {}
    for r in vvu:
        rows = read_csv(os.path.join(gloss, r["reference_data_file"]))
        vv[(r["table"], r["column"])] = {
            "list": os.path.splitext(os.path.basename(r["reference_data_file"]))[0],
            "values": [[x["code"], x["value"]] for x in rows]}

    def areas(t):
        r = terms.get(t)
        if not r:
            return []
        c = [r["Category"]] + ([r["Secondary Categories"]] if r["Secondary Categories"] else [])
        c += [e["secondary_category"] for e in ex if e["term"] == t]
        out = []
        for x in c:
            x = x.split(">> ")[-1]
            if x and x not in out:
                out.append(x)
        return [x for x in out if x not in roots]  # filed under the root only: no subject area

    def cls(t):
        c = cs.get(t) if t else None
        return {"class": c["Classifications"], "conf": c["confidence"], "reason": c["reason"]} if c else None

    data = {"title": title, "model": m["model"],
            "subject_areas": [r["Name"] for r in g if r["Artifact Type"] == "category" and r["Category"]],
            "tables": [], "views": [], "rels": [],
            "domains": [d["name"] for d in m.get("domains", []) if not d["name"].startswith("<")],
            "vv_lists": len({v["list"] for v in vv.values()})}
    for e in m["entities"]:
        tt = term_of.get((e["table"], ""))
        cols = []
        for c in e["columns"]:
            t = term_of.get((e["table"], c["column"]))
            cols.append({"name": c["column"], "attr": c["attribute"], "type": c["datatype"],
                         "pk": c["primary_key"], "fk": c["foreign_key"], "def": c["definition"],
                         "term": t, "abbr": terms.get(t, {}).get("Abbreviations", "") if t else "",
                         "cls": cls(t), "vv": vv.get((e["table"], c["column"]))})
        data["tables"].append({
            "name": e["table"], "entity": e["entity"], "def": e["definition"], "term": tt,
            "abbr": terms.get(tt, {}).get("Abbreviations", "") if tt else "",
            "areas": areas(tt) if tt else [], "props": props.get(e["table"], {}),
            "keys": [{"type": k["type"], "columns": k["columns"]} for k in e.get("keys", [])],
            "cols": cols})
    for v in m.get("views", []):
        data["views"].append({
            "name": v["name"], "entity": v["view"], "def": v["definition"],
            "sources": [s["table"] for s in v["sources"]],
            "aliases": {s["alias"]: s["table"] for s in v["sources"]},
            "joins": [f"{o[0]}.{o[1]} = {o[2]}.{o[3]}" for j in v.get("joins", []) for o in j["on"]],
            "cols": [{"name": c["column"], "sql": c["sql"], "def": c["definition"],
                      "term": term_of.get((v["name"], c["column"]))} for c in v["columns"]]})
    names = {t["name"] for t in data["tables"]}
    for r in m["relationships"]:
        if r["parent"] in names and r["child"] in names:
            data["rels"].append({k: r.get(k) for k in ("name", "parent", "child", "phrase", "inverse_phrase",
                                                      "definition", "child_columns", "parent_columns")})
    return data


def height(o, is_view):
    if is_view:
        return HDR + 18 * (len(o["sources"]) + 1) + 12
    return HDR + ROW * len(o["cols"]) + 12


def auto_layout(data):
    """Layered left-to-right layout: parents left of children, views last."""
    tables = {t["name"]: t for t in data["tables"]}
    parents = defaultdict(set)
    for r in data["rels"]:
        if r["parent"] != r["child"]:
            parents[r["child"]].add(r["parent"])

    rank, state = {}, {}

    def depth(n):  # longest parent chain; cycles are cut
        if n in rank:
            return rank[n]
        if state.get(n) == "busy":
            return 0
        state[n] = "busy"
        rank[n] = max((depth(p) + 1 for p in parents[n]), default=0)
        return rank[n]

    for n in tables:
        depth(n)
    # Unrelated tables would all pile into rank 0; spread them over the columns.
    lonely = [n for n in tables if not parents[n] and not any(n in parents[c] for c in tables)]
    ncols = max(rank.values(), default=0) + 1
    for i, n in enumerate(sorted(lonely)):
        rank[n] = i % max(ncols, 3)

    pos, by_rank = {}, defaultdict(list)
    for n, r in rank.items():
        by_rank[r].append(n)
    for r in sorted(by_rank):
        def bary(n):
            ps = [pos[p][1] for p in parents[n] if p in pos]
            return sum(ps) / len(ps) if ps else float("inf")
        y = MARGIN
        for n in sorted(by_rank[r], key=lambda n: (bary(n), n)):
            pos[n] = [MARGIN + r * COL_STEP, y]
            y += height(tables[n], False) + GAP
    vx = MARGIN + (max(by_rank, default=-1) + 1) * COL_STEP
    y = MARGIN
    for v in data["views"]:
        pos[v["name"]] = [vx, y]
        y += height(v, True) + GAP
    return pos


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("model", help="path to the .erwin file")
    ap.add_argument("--out", help="HTML file to write (default: <model>.diagram.html next to the model)")
    ap.add_argument("--title", help="page title (default: model file name)")
    ap.add_argument("--layout", help="JSON {name: [x, y]} positions; missing names are auto-placed. "
                                     "Default: assets/layouts/<model file stem>.json when present; 'none' for pure auto-layout")
    ap.add_argument("--save-layout", help="also write the positions used, for hand-tuning")
    ap.add_argument("--select", help="table or view selected when the page opens")
    a = ap.parse_args()

    stem = os.path.splitext(os.path.basename(a.model))[0]
    title = a.title or stem
    with tempfile.TemporaryDirectory() as tmp:
        m, gloss = export(a.model, tmp)
        data = build_data(m, gloss, title)

    pos = auto_layout(data)
    layout_file = a.layout or os.path.join(LAYOUTS, re.sub(r"\W+", "_", stem.lower()) + ".json")
    if a.layout != "none" and os.path.exists(layout_file):
        with open(layout_file, encoding="utf-8") as f:
            given = json.load(f)
        pos.update({k: v for k, v in given.items() if k in pos})
        sys.stderr.write(f"layout: {layout_file}\n")
    data["layout"] = pos
    if a.save_layout:
        with open(a.save_layout, "w", encoding="utf-8") as f:
            json.dump(pos, f, indent=1)

    if a.select and a.select in pos:
        data["default_selected"] = a.select
    else:  # the most connected table
        deg = defaultdict(lambda: [0, 0])  # [relationships, as child]
        for r in data["rels"]:
            deg[r["parent"]][0] += 1
            deg[r["child"]][0] += 1
            deg[r["child"]][1] += 1
        data["default_selected"] = max(deg, key=lambda n: deg[n]) if deg else (data["tables"] or data["views"])[0]["name"]

    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__TITLE__", title.replace("<", "&lt;")).replace("__DATA__", blob)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.model)), stem + ".diagram.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    n_cols = sum(len(t["cols"]) for t in data["tables"])
    print(f"{data['model']}: {len(data['tables'])} tables ({n_cols} columns), {len(data['rels'])} relationships, "
          f"{len(data['views'])} views, {len(data['subject_areas'])} subject areas -> {out}")


if __name__ == "__main__":
    main()
