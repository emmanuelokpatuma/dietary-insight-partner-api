"""Entry point for uvicorn / Cloud Run: `uvicorn partner_api.server:app`."""
from .main import create_app

app = create_app()
