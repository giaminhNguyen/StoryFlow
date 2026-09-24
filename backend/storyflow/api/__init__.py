"""Phase 6 local HTTP API: a thin adapter over the Phase 5 application layer.

Handlers only call ``WorkflowService`` / ``RunnerService`` (mutations) and ``ReadModels``
(queries), reachable through ``request.app.state.container``. They never touch ORM models,
never enqueue jobs and never call runners. See ``errors.py`` for the error contract.
"""
