from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.endpoints import router

app = FastAPI(
    title="pdfshield",
    description="PDF forensic analysis and risk detection API",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

app.include_router(router, prefix="/api")
