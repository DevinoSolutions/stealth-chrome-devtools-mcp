"""Allow running as `python -m stealth_chrome_devtools_mcp`.

The guard is load-bearing and not boilerplate (F-904). Without it, IMPORTING
this module is indistinguishable from RUNNING the product: the body called
``main()``, so any tool that walks the package module by module -- a doc
generator, an import linter, a coverage sweep, an IDE -- started a stdio proxy
and cold-started a backend. That is not hypothetical; this finding's own census
probe did exactly that, into the operator's live ``~/.stealth-mcp`` (F-903's,
which is how the two findings are related).

``python -m stealth_chrome_devtools_mcp`` runs this file under the name
``__main__``, so the one door that should start the server still does.
``tests/test_package_entrypoints.py`` pins both halves, plus the rule that
generalises the defect: no module body in this package may CALL anything.
"""

from stealth_chrome_devtools_mcp.server import main

if __name__ == "__main__":
    main()
