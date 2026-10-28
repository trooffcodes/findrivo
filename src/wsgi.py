"""WSGI entry point for production servers.

Example:
    PYTHONPATH=src gunicorn --workers 1 --threads 8 --timeout 300 wsgi:app
"""

from app import app

__all__ = ["app"]
