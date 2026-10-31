"""The internal operations panel (architecture.md §19): FastAPI, Jinja2 and HTMX, no build step.

A process of its own (``uvicorn kalkan.ops.main:app``, port 8081), bound to localhost and
reached through an IAP tunnel in prod. It reads every tenant as kalkan_worker; each write is
an ``audit_log`` row.
"""
