# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""pytest plugin that records the arguments and the outcome of every Spec.splice call a test
makes, as JSON in the file named by SPLICE_CAPTURE. See splice_review_page.py."""

import json
import os

import pytest

import spack.deptypes as dt
import spack.spec

ORIGINAL = spack.spec.Spec.splice
ITEMS = {}
STATE = {"item": None, "depth": 0}


def snapshot(root):
    """Nodes and edges reachable from root, nodes identified by object"""
    order, index = [], {}
    queue = [root]
    while queue:
        node = queue.pop(0)
        if id(node) in index:
            continue
        index[id(node)] = len(order)
        order.append(node)
        queue.extend(e.spec for e in node.edges_to_dependencies())
    nodes = [
        {"name": n.name, "version": str(n.version), "spliced": bool(n.concrete and n.spliced)}
        for n in order
    ]
    edges = [
        {
            "parent": index[id(n)],
            "child": index[id(e.spec)],
            "deptypes": list(dt.flag_to_tuple(e.depflag)),
            "virtuals": list(e.virtuals),
        }
        for n in order
        for e in n.edges_to_dependencies()
    ]
    return {"nodes": nodes, "edges": edges}


def recording(self, other, transitive=True, **kwargs):
    if STATE["depth"]:
        return ORIGINAL(self, other, transitive, **kwargs)
    call = {
        "spec": snapshot(self),
        "replacement": snapshot(other),
        "transitive": transitive,
        "kwargs": {k: str(v) for k, v in kwargs.items()},
    }
    ITEMS[STATE["item"]]["calls"].append(call)
    STATE["depth"] += 1
    try:
        out = ORIGINAL(self, other, transitive, **kwargs)
    except Exception as e:
        call["error"] = [type(e).__name__, str(e)]
        raise
    finally:
        STATE["depth"] -= 1
    call["result"] = snapshot(out)
    return out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    STATE["item"] = item.nodeid
    ITEMS[item.nodeid] = {"calls": []}
    spack.spec.Spec.splice = recording
    yield
    spack.spec.Spec.splice = ORIGINAL


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    if report.when == "call":
        ITEMS[item.nodeid]["outcome"] = report.outcome
        ITEMS[item.nodeid]["xfail"] = getattr(report, "wasxfail", None)


def pytest_sessionfinish(session):
    with open(os.environ["SPLICE_CAPTURE"], "w") as f:
        json.dump(ITEMS, f, indent=1)
