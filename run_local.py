"""Run the ingest API locally. The Visual Studio F5 startup item (plan §10.5).

Visual Studio's Open Folder mode debugs a Python *file*, not a uvicorn command line, so
this file is the thing to set as the startup item: right-click it in Solution Explorer,
choose "Set as Startup Item", press F5, and breakpoints anywhere under ``src/leadquali/``
are hit normally. The interactive API docs are then at http://127.0.0.1:8000/docs.

``reload=True`` runs the app in a child process, which can detach the debugger when it
restarts. If breakpoints stop being hit, set ``reload=False`` for the session.

Equivalent from a shell: ``uvicorn leadquali.api.main:app --reload``.
"""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("leadquali.api.main:app", host="127.0.0.1", port=8000, reload=True)
