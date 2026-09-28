#!/usr/bin/env python3
"""wq#2895 class sweep: find functions that open a sqlite connection and do not
close it on EVERY exit path (no enclosing try/finally close, no `with`).

Flags per function:
  UNGUARDED   connect assigned to a name, close() exists only outside any finally
  NO_CLOSE    connect assigned, never closed in this function (returned or leaked)
  RETURNED    the connection object is returned (a factory -- caller owns it; not a leak)
Usage: sqlite_leak_scan.py <file-or-dir> ...
"""
import ast, os, sys

CONNECT_ATTRS = {"connect", "_connect", "_db", "db", "get_conn", "_conn", "conn_rw", "open_db"}


def is_connect_call(node):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "connect" and isinstance(f.value, ast.Name) \
            and f.value.id == "sqlite3":
        return True
    if isinstance(f, ast.Name) and f.id in CONNECT_ATTRS:
        return True
    if isinstance(f, ast.Attribute) and f.attr in CONNECT_ATTRS and not (
            isinstance(f.value, ast.Name) and f.value.id == "sqlite3"):
        return True
    return False


def names_closed(stmts):
    out = set()
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "close" and isinstance(n.func.value, ast.Name):
                out.add(n.func.value.id)
            # helper close: _close(con) / close_quiet(con)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and "close" in n.func.id:
                for a in n.args:
                    if isinstance(a, ast.Name):
                        out.add(a.id)
    return out


def scan_func(fn, path):
    findings = []
    opened = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and is_connect_call(n.value):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    opened.setdefault(t.id, n.lineno)
    if not opened:
        return findings
    finally_closed, any_closed, returned = set(), set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Try) and n.finalbody:
            finally_closed |= names_closed(n.finalbody)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Name):
            returned.add(n.value.id)
    any_closed = names_closed(fn.body)
    for name, line in opened.items():
        if name in finally_closed:
            continue
        if name in returned:
            kind = "RETURNED"
        elif name in any_closed:
            kind = "UNGUARDED"
        else:
            kind = "NO_CLOSE"
        findings.append((path, line, fn.name, name, kind))
    return findings


def scan_rollback(fn, path):
    """NO_ROLLBACK (wq#2895 follow-up, found live 2026-09-28 06:01Z in
    lumen_scout_expanded.insert_intel): a try whose body commit()s on a connection
    and whose except handler swallows the error (no raise) without rollback()/close()
    on that connection. Python's implicit BEGIN stays open after a failed INSERT or
    COMMIT, so a long-lived connection keeps SHARED/RESERVED for the rest of the run."""
    findings = []
    # A connection closed by ANY finally that encloses this try, or that is nested
    # inside it, has its transaction ended (close() rolls back) before the swallowing
    # handler can leave it open -- not a leak.
    finally_scopes = []   # (set of node ids under the try, names its finally closes)
    for t2 in ast.walk(fn):
        if isinstance(t2, ast.Try) and t2.finalbody:
            under = {id(n) for n in ast.walk(t2)}
            finally_scopes.append((t2, under, names_closed(t2.finalbody)))
    for t in ast.walk(fn):
        if not isinstance(t, ast.Try) or not t.handlers:
            continue
        committed = set()
        for n in ast.walk(ast.Module(body=t.body, type_ignores=[])):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "commit" and isinstance(n.func.value, ast.Name):
                committed.add(n.func.value.id)
        if not committed:
            continue
        t_nodes = {id(n) for n in ast.walk(t)}
        for t2, under, closed in finally_scopes:
            if id(t) in under or id(t2) in t_nodes:     # encloses t, is t, or nested in t
                committed -= closed
        if not committed:
            continue
        for h in t.handlers:
            hn = list(ast.walk(ast.Module(body=h.body, type_ignores=[])))
            if any(isinstance(n, ast.Raise) for n in hn):
                continue
            undone = set()
            for n in hn:
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr in ("rollback", "close") and isinstance(n.func.value, ast.Name):
                    undone.add(n.func.value.id)
            for name in sorted(committed - undone):
                findings.append((path, t.lineno, fn.name, name, "NO_ROLLBACK"))
    return findings


def scan_file(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
    except Exception as e:
        return [(path, 0, "<parse>", "", "PARSE_ERROR %s" % e)]
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out += scan_func(n, path)
            out += scan_rollback(n, path)
    return out


def main(argv):
    files = []
    for a in argv:
        if os.path.isdir(a):
            for root, dirs, fs in os.walk(a):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv", ".venv",
                                                        "site-packages", "node_modules")]
                files += [os.path.join(root, f) for f in fs if f.endswith(".py")]
        else:
            files.append(a)
    rows = []
    for f in sorted(files):
        rows += scan_file(f)
    for r in rows:
        print("%-9s %s:%d  %s()  var=%s" % (r[4], r[0], r[1], r[2], r[3]))
    print("TOTAL", len(rows), "UNGUARDED", sum(r[4] == "UNGUARDED" for r in rows),
          "NO_CLOSE", sum(r[4] == "NO_CLOSE" for r in rows),
          "NO_ROLLBACK", sum(r[4] == "NO_ROLLBACK" for r in rows),
          "RETURNED", sum(r[4] == "RETURNED" for r in rows))


if __name__ == "__main__":
    main(sys.argv[1:])
