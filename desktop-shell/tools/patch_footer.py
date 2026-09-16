"""Remove the bottom-left sidebar footer links (official site + GitHub) from
the compiled deeptutor_web Next.js bundle (footer-bearing module copies only):

  - static/chunks/1252-bbda903e8f5a602c.js   (client hydration)  ns='a', site var 'er', gh comp 'ea'
  - server/chunks/6653.js                    (SSR mirror)        ns='d', site var '_', gh comp 'aa'

Each footer renders two icon links:
  (0,<ns>.jsx)("a",{href:<site>, ... "Docs" official site icon ...})
  (0,<ns>.jsx)(<gh>,{className:"..."})        GitHub octocat icon
We remove both (element + comma collapse), then neutralize URL constants.
"""
from __future__ import annotations
import re

JOBS = [
    (r"C:\Users\frank\DeepTutorDesktop\runtime-build\staging\python\Lib\site-packages\deeptutor_web\.next\static\chunks\1252-bbda903e8f5a602c.js", "a", "er", "ea"),
    (r"C:\Users\frank\DeepTutorDesktop\runtime-build\staging\python\Lib\site-packages\deeptutor_web\.next\server\chunks\6653.js", "d", "_", "aa"),
]


def patch(path: str, ns: str, site_var: str, gh_comp: str) -> None:
    src = open(path, encoding="utf-8").read()
    orig = src

    # 1) official-site ("Docs") anchors — fixed element tail
    docs_re = re.compile(
        re.escape('(0,%s.jsx)("a",{href:%s,target:"_blank"' % (ns, site_var))
        + r'.*?text-blue-600 dark:text-blue-400"}\)\}\)',
        re.S,
    )
    src, n_docs = docs_re.subn("", src)
    print(f"  removed docs anchors: {n_docs}")

    # 2) GitHub icon usages — props are either {className:"..."} or {}
    gh_re = re.compile(
        re.escape("(0,%s.jsx)(%s,{" % (ns, gh_comp)) + r'(?:className:"[^"]*")?\}\)'
    )
    src, n_gh = gh_re.subn("", src)
    print(f"  removed github icon usages: {n_gh}")

    # 3) neutralize official-site URL constant (in case a usage remains)
    src = src.replace('let %s="https://deeptutor.info/";' % site_var, 'let %s="#";' % site_var)

    # 4) neutralize any remaining hardcoded github href (icon definition)
    src = src.replace('href:"https://github.com/HKUDS/DeepTutor"', 'href:"#"')

    # 5) collapse commas that removal left behind inside children arrays
    before = src
    src = src.replace(",,", ",").replace("(,", "(").replace("[,", "[").replace(",]", "]")
    print(f"  comma collapse applied: {src != before}")

    open(path, "w", encoding="utf-8").write(src)
    print(f"  {path}  ({len(orig)} -> {len(src)} bytes)")


if __name__ == "__main__":
    for path, ns, sv, gc in JOBS:
        print("PATCH", path)
        patch(path, ns, sv, gc)
    print("done")
