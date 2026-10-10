# sitecustomize.py -- loaded by every Python in this venv at startup (VPS only).
# A freshly installed Windows Server has an incomplete trusted-root store, so urllib /
# websocket connections (Delta, Fyers websocket) failed with CERTIFICATE_VERIFY_FAILED.
# Point OpenSSL at certifi's Mozilla bundle unless something else already chose a bundle.
import os
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass
