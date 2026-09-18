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

#: The billing Lambda's entry point: ``leadquali.api.handlers.billing_handler``.
#:
#: The **same** ASGI app, wrapped a second time, because API Gateway routes by path and
#: only ``/webhooks/stripe`` and ``/billing/portal`` are pointed here. One app keeps "one
#: deployment" true; two functions keep the *permissions* apart, which is the half that
#: matters: the Stripe API key and the ``whsec_`` never reach the function that serves a
#: customer's web form, an error-rate alarm on billing cannot be set off by a bot probing
#: ``/leads``, and a bad billing deploy cannot take down the one surface where a failure
#: loses a lead.
#:
#: Dependencies are built lazily per surface (see
#: :func:`leadquali.api.webhooks._default_billing_deps`), so the ingest function never
#: constructs a Stripe client and the billing function never constructs a lead queue —
#: whichever of the two a given container happens to be.
billing_handler = Mangum(app, lifespan="off")

__all__ = ["billing_handler", "ingest_handler"]
