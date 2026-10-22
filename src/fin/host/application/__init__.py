"""Application capability: composition and resource lifetime.

The boundary is the package; the two modules inside it name what it
owns. ``lifetime`` holds the resource lifetime, the shutdown ordering
and the single-process guard; ``composition`` holds the boot seams and
the host-graph composition both entry points reach.
"""
