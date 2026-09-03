"""Lambda entrypoints. The ASGI app stays deployment-agnostic; this file is the adapter.

`Mangum <https://mangum.io>`_ translates an API Gateway (or Lambda function URL) event into
an ASGI scope and the ASGI response back into a Lambda result, so the same
:data:`leadquali.api.main.app` that uvicorn serves on a laptop runs unchanged behind API
Gateway. That is the point of keeping this in its own module: ``api/main.py`` has no idea
it might be running in Lambda, and swapping to a container or to a plain ASGI server is
deleting a file rather than unpicking a framework.

``lifespan="off"`` because the app has no startup or shutdown hooks and a Lambda cold start
should not wait for a lifespan protocol it does not use. Dependencies are built lazily on
the first request (see :func:`leadquali.api.main.create_app`), which is what makes that
safe: a cold start imports the module and nothing else, and a container reused for a
thousand invocations builds its database engine once.

#26 owns the qualification worker's handler and the SQS wiring; this file is only the
ingest side.
"""

from __future__ import annotations

from mangum import Mangum

from leadquali.api.main import app

#: The ingest Lambda's entry point: ``leadquali.api.handlers.ingest_handler``.
ingest_handler = Mangum(app, lifespan="off")

__all__ = ["ingest_handler"]
