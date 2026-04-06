"""Start the Trading 212 dashboard web server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.server:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
    )
