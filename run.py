"""Start the SPTI Payroll web server: python run.py  ->  http://<this-pc-ip>:8000"""
import logging
import os
import socket

from waitress import serve

from payroll.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app = create_app(start_sync=True)
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "localhost"
    print(f"SPTI Payroll running at http://localhost:{port}  (LAN: http://{ip}:{port})")
    serve(app, host="0.0.0.0", port=port, threads=8)
