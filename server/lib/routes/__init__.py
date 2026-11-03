"""HTTP route layer of the Clarp server (refactor slice S3).

lib.routes is the HTTP adapter layer: it may call Handler methods. No other
module in server/lib imports it, and it never imports server.py.
See docs/architecture/route-registry.md.
"""
