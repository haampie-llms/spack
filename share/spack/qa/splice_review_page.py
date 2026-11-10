# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Review page for the Spec.splice tests: for each test, the spec and the replacement it builds,
and what Spec.splice returned in each recorded run.

Record the calls with the pytest plugin splice_capture.py, once per checkout to compare, and
build the page from the recordings. Graphviz (dot) lays out the graphs.

    export PYTHONPATH=share/spack/qa
    SPLICE_CAPTURE=develop.json python3 -m pytest -p splice_capture \\
        lib/spack/spack/test/spec_splice.py

    # in a checkout with the fix, on the same test file
    SPLICE_CAPTURE=fix.json python3 -m pytest -p splice_capture --runxfail \\
        lib/spack/spack/test/spec_splice.py

    python3 share/spack/qa/splice_review_page.py lib/spack/spack/test/spec_splice.py \\
        "on develop=develop.json" "with the fix=fix.json" > page.html
"""

import ast
import collections
import html
import json
import shlex
import subprocess
import sys

esc = html.escape


def node_label(node):
    return f"{node['name']}@{node['version']}"


def edge_label(edge):
    """The virtuals, and the dependency types unless it is a plain link dependency"""
    label = ", ".join(edge["virtuals"])
    if edge["deptypes"] != ["link"]:
        label = f"{label} ({', '.join(edge['deptypes'])})".strip()
    return label


def describe(graph):
    """The graph in words, as text alternative"""
    nodes = graph["nodes"]
    if not graph["edges"]:
        return ", ".join(node_label(n) for n in nodes)

    def arrow(e):
        text = f"{node_label(nodes[e['parent']])} -> {node_label(nodes[e['child']])}"
        return f"{text} {edge_label(e)}".strip()

    return "; ".join(arrow(e) for e in graph["edges"])


def canonical(graph):
    """The graph with its nodes in breadth-first order and children ordered by name. Specs store
    edges in the order they were added, which differs between results: without this the same DAG
    is drawn in different ways."""
    nodes = graph["nodes"]
    children = collections.defaultdict(list)
    for e in graph["edges"]:
        children[e["parent"]].append(e)
    order, queue = [], [0]
    while queue:
        i = queue.pop(0)
        if i not in order:
            order.append(i)
            children[i].sort(key=lambda e: (node_label(nodes[e["child"]]), edge_label(e)))
            queue.extend(e["child"] for e in children[i])
    index = {old: new for new, old in enumerate(order)}
    edges = [
        {**e, "parent": index[e["parent"]], "child": index[e["child"]]}
        for i in order
        for e in children[i]
    ]
    return {"nodes": [nodes[i] for i in order], "edges": edges}


def svg(graph):
    """Draw a recorded graph, with positions from dot"""
    graph = canonical(graph)
    nodes = graph["nodes"]
    dot = [
        "digraph {",
        "rankdir=TB; nodesep=0.3; ranksep=0.5; ordering=out;",
        "node [shape=box, fixedsize=true, height=0.42, fontsize=10];",
        "edge [arrowhead=none, fontsize=9];",
    ]
    for i, n in enumerate(nodes):
        width = (len(node_label(n)) * 7.4 + 22) / 72
        dot.append(f'n{i} [width={width:.3f}, label="{node_label(n)}"];')
    pending = collections.defaultdict(list)
    for e in graph["edges"]:
        dot.append(f'n{e["parent"]} -> n{e["child"]} [label="{edge_label(e)} "];')
        pending[(e["parent"], e["child"])].append(e)
    dot.append("}")
    plain = subprocess.run(
        ["dot", "-Tplain"], input="\n".join(dot), text=True, capture_output=True, check=True
    ).stdout

    # plain output is in inches with the origin at the bottom left
    pad, scale = 8, 72
    width = height = 0.0
    paths, boxes, labels = [], [], []
    for line in plain.splitlines():
        tok = shlex.split(line)
        if tok[0] == "graph":
            width, height = float(tok[2]) * scale, float(tok[3]) * scale
        elif tok[0] == "node":
            n = nodes[int(tok[1][1:])]
            x, y = float(tok[2]) * scale + pad, height - float(tok[3]) * scale + pad
            w, h = float(tok[4]) * scale, float(tok[5]) * scale
            boxes.append(
                f'<rect class="{"n spliced" if n["spliced"] else "n"}" x="{x - w / 2:.1f}" '
                f'y="{y - h / 2:.1f}" width="{w:.1f}" height="{h:.1f}" rx="5"/>'
                f'<text class="nt" x="{x:.1f}" y="{y + 4.2:.1f}" text-anchor="middle">'
                f"{esc(node_label(n))}</text>"
            )
        elif tok[0] == "edge":
            e = pending[(int(tok[1][1:]), int(tok[2][1:]))].pop(0)
            count = int(tok[3])
            points = [
                (float(tok[4 + 2 * i]) * scale + pad, height - float(tok[5 + 2 * i]) * scale + pad)
                for i in range(count)
            ]
            curve = " ".join(f"{x:.1f} {y:.1f}" for x, y in points[1:])
            build_only = not {"link", "run"} & set(e["deptypes"])
            paths.append(
                f'<path class="{"e build" if build_only else "e"}" marker-end="url(#arrow)" '
                f'd="M{points[0][0]:.1f} {points[0][1]:.1f}C{curve}"/>'
            )
            if edge_label(e):
                x = float(tok[5 + 2 * count]) * scale + pad
                y = height - float(tok[6 + 2 * count]) * scale + pad
                labels.append(
                    f'<text class="el" x="{x:.1f}" y="{y + 3.5:.1f}" text-anchor="middle">'
                    f"{esc(edge_label(e))}</text>"
                )
    w, h = width + 2 * pad, height + 2 * pad
    return (
        f'<svg viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}" role="img" '
        f'aria-label="{esc(describe(graph))}">{"".join(paths + boxes + labels)}</svg>'
    )


def panel(title, body):
    return f'<figure class="panel"><figcaption>{esc(title)}</figcaption>{body}</figure>'


def outcome(call):
    if "error" in call:
        kind, message = call["error"]
        return f'<p class="err"><b>{esc(kind)}</b> {esc(message)}</p>'
    return svg(call["result"])


def tests(source):
    """Name, docstring and source of each test, in file order"""
    lines = source.splitlines()
    for fn in ast.parse(source).body:
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_"):
            start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
            yield (
                fn.name,
                ast.get_docstring(fn) or fn.name,
                "\n".join(lines[start - 1 : fn.end_lineno]),
            )


def section(name, doc, source, runs):
    """One test: its inputs, then per call the result of every run"""
    first_run = next(iter(runs.values()))
    items = [k for k in first_run if k.split("::")[-1].split("[")[0] == name]
    assert items and all(k in run for run in runs.values() for k in items), name

    badges, failing = "", False
    for i, (label, run) in enumerate(runs.items()):
        xfail = next((run[k]["xfail"] for k in items if run[k]["xfail"] is not None), None)
        fails = xfail is not None or any(run[k]["outcome"] == "failed" for k in items)
        failing = failing or (i == 0 and fails)
        reason = f": {xfail.replace('reason: ', '')}" if xfail else ""
        text = f"fails {label}{reason}" if fails else f"passes {label}"
        badges += f'<span class="badge {"fails" if fails else "passes"}">{esc(text)}</span>'

    # inputs are shown once if every call of every run has the same ones
    first = first_run[items[0]]["calls"][0]
    same_inputs = all(
        len(run[k]["calls"]) == 1
        and (c["spec"], c["replacement"]) == (first["spec"], first["replacement"])
        for run in runs.values()
        for k in items
        for c in run[k]["calls"]
    )
    rows = []
    if same_inputs:
        inputs = panel("spec", svg(first["spec"])) + panel(
            "replacement", svg(first["replacement"])
        )
        rows.append(f'<div class="row">{inputs}</div>')
    for k in items:
        calls = [run[k]["calls"] for run in runs.values()]
        assert len({len(c) for c in calls}) == 1, k
        for i, same_call in enumerate(zip(*calls)):
            call = same_call[0]
            assert all(
                (c["replacement"], c["transitive"]) == (call["replacement"], call["transitive"])
                for c in same_call
            ), k
            kwargs = "".join(f", {key}={value}" for key, value in call["kwargs"].items())
            receiver = "spec" if i == 0 else "result"
            text = f"{receiver}.splice(replacement, transitive={call['transitive']}{kwargs})"
            rows.append(f'<p class="call"><code>{esc(text)}</code></p>')
            if not same_inputs:
                if all(c["spec"] == call["spec"] for c in same_call):
                    spec = panel("spec", svg(call["spec"]))
                else:
                    spec = panel("spec", '<p class="note">the result of the call above</p>')
                replacement = panel("replacement", svg(call["replacement"]))
                rows.append(f'<div class="row">{spec}{replacement}</div>')
            results = "".join(
                panel(f"result {label}", outcome(c)) for label, c in zip(runs, same_call)
            )
            rows.append(f'<div class="row">{results}</div>')

    index = f'<li class="{"fails" if failing else "passes"}"><a href="#{name}">{esc(doc)}</a></li>'
    return index, (
        f'<section id="{name}"><h2>{esc(doc)}</h2>'
        f'<p class="meta"><code>{name}</code>{badges}</p>{"".join(rows)}'
        f"<details><summary>Test source</summary><pre><code>{esc(source)}</code></pre></details>"
        "</section>"
    )


CSS = """
:root {
  --bg: #f5f7f6; --fg: #16201c; --muted: #55645e; --panel: #e9eeec; --node: #ffffff;
  --rule: #cfd8d4; --spliced: #1a5fd0; --spliced-bg: #e3edff; --fails: #a84a0c;
  --fails-bg: #fbeee2; --passes: #1d6b45; --passes-bg: #dff1e7;
  --sans: system-ui, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111815; --fg: #e4ebe7; --muted: #94a49d; --panel: #19231f; --node: #232e29;
    --rule: #33413b; --spliced: #86b4ff; --spliced-bg: #16294a; --fails: #f0a468;
    --fails-bg: #3a2414; --passes: #7fd3a4; --passes-bg: #143324; color-scheme: dark;
  }
}
body { margin: 0; background: var(--bg); color: var(--fg); font: 400 16px/1.55 var(--sans); }
main { max-width: 1040px; margin: 0 auto; padding: 40px 16px 64px; display: flex;
  flex-direction: column; gap: 36px; }
header { display: flex; flex-direction: column; gap: 12px; }
header p { max-width: 70ch; margin: 0; }
h1 { margin: 0; font: 600 28px/1.2 var(--sans); }
h2 { margin: 0; font: 600 19px/1.3 var(--sans); }
code { font: 500 .9em var(--mono); }
a { color: var(--spliced); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 22px; margin: 0; padding: 0; list-style: none;
  font-size: 14px; color: var(--muted); }
.legend svg { vertical-align: -3px; margin-right: 6px; }
.index { margin: 0; padding: 0; list-style: none; columns: 2 320px; column-gap: 32px;
  font-size: 14.5px; }
.index li { break-inside: avoid; padding: 2px 0 2px 16px; position: relative; }
.index li::before { content: ""; position: absolute; left: 0; top: .65em; width: 8px; height: 8px;
  border-radius: 50%; background: var(--fails); }
.index li.passes::before { background: var(--passes); }
section { display: flex; flex-direction: column; gap: 12px; padding-top: 28px;
  border-top: 1px solid var(--rule); }
.meta { margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px;
  font-size: 14px; color: var(--muted); }
.meta code { overflow-wrap: anywhere; }
.badge { font-size: 12.5px; font-weight: 500; padding: 2px 8px; border-radius: 4px; }
.badge.fails { color: var(--fails); background: var(--fails-bg); }
.badge.passes { color: var(--passes); background: var(--passes-bg); }
.row { display: flex; flex-wrap: wrap; gap: 12px; }
.call { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.panel { margin: 0; flex: 1 1 280px; min-width: 0; background: var(--panel); border-radius: 8px;
  padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 8px; }
.panel figcaption { font: 500 12px var(--mono); letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); }
.panel svg { display: block; max-width: 100%; height: auto; margin: 0 auto; }
.note, .err { margin: 0; font-size: 14px; color: var(--muted); }
.err { color: var(--fails); overflow-wrap: anywhere; }
.err b { display: block; }
details { font-size: 14px; }
summary { cursor: pointer; color: var(--muted); width: fit-content; }
pre { margin: 8px 0 0; padding: 12px 14px; background: var(--panel); border-radius: 8px;
  overflow-x: auto; font: 400 12.5px/1.5 var(--mono); }
pre code { font: inherit; }
.n { fill: var(--node); stroke: var(--fg); stroke-width: 1.2; }
.n.spliced { fill: var(--spliced-bg); stroke: var(--spliced); stroke-width: 1.8; }
.nt { fill: var(--fg); font: 500 12px var(--mono); }
.e { fill: none; stroke: var(--fg); stroke-width: 1.2; }
.e.build { stroke-dasharray: 5 4; }
.el { fill: var(--muted); font: 400 10.5px var(--sans); }
.ah { fill: var(--fg); }
"""


def page(test_file, runs):
    with open(test_file, encoding="utf-8") as f:
        source = f.read()
    parts = [section(name, doc, src, runs) for name, doc, src in tests(source)]
    index = "".join(i for i, _ in parts)
    sections = "".join(s for _, s in parts)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spec.splice test DAGs</title>
<style>{CSS}</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<marker id="arrow" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="7" markerHeight="7"
  orient="auto"><path class="ah" d="M0 0L10 5L0 10z"/></marker>
</defs></svg>
<main>
<header>
<h1>The DAGs in the Spec.splice tests</h1>
<p>Each test in <code>{esc(test_file)}</code> builds a spec and a replacement and splices them.
This page shows those inputs and what <code>Spec.splice</code> did with them
{esc(" and ".join(runs))}. The graphs are the recorded arguments and results of the calls, laid
out by Graphviz.</p>
<ul class="legend">
<li><svg width="30" height="14" viewBox="0 0 30 14" aria-hidden="true"><rect class="n spliced"
  x="1" y="1" width="28" height="12" rx="3"/></svg>spliced node: it has a build spec</li>
<li><svg width="30" height="14" viewBox="0 0 30 14" aria-hidden="true"><path class="e build"
  d="M1 7H29"/></svg>build-only dependency</li>
<li>edge label: the virtuals a dependency is used as; dependency types other than link in
brackets</li>
</ul>
</header>
<nav aria-label="Tests"><ul class="index">{index}</ul></nav>
{sections}
</main>
</body>
</html>
"""


def main():
    if len(sys.argv) < 3 or any("=" not in arg for arg in sys.argv[2:]):
        sys.exit(f"usage: {sys.argv[0]} TEST_FILE 'LABEL=RECORDING.json'...")
    runs = {}
    for arg in sys.argv[2:]:
        label, _, path = arg.rpartition("=")
        with open(path, encoding="utf-8") as f:
            runs[label] = json.load(f)
    sys.stdout.write(page(sys.argv[1], runs))


if __name__ == "__main__":
    main()
