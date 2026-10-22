"""
Launcher for Centralized Audio Research & Revalidation Workflow
Binds to 0.0.0.0 to enable local network access (team sharing on same Wi-Fi / LAN).
"""

import socket
import uvicorn
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
    if os.path.exists("backend/.env"):
        load_dotenv("backend/.env", override=True)
except ImportError:
    pass

def get_local_network_ip():
    """Finds the local IP address on the Wi-Fi/LAN network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def find_available_port(start_port=8088, max_attempts=50):
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return start_port

if __name__ == "__main__":
    local_ip = get_local_network_ip()
    port = find_available_port(8088)

    print("=" * 70)
    print("  Centralized Audio Research & Revalidation Workflow")
    print("  Focus: Indian English Speech with Spoken Sanskrit Terms")
    print("=" * 70)
    print(f"  * Local Access:     http://localhost:{port}")
    print(f"  * Share with Team:  http://{local_ip}:{port}")
    print("=" * 70)
    print("  (Anyone connected to the same Wi-Fi/network can use this link)")
    print("=" * 70)

    # Bind to 0.0.0.0 so all network devices on the same Wi-Fi/LAN can connect
    uvicorn.run("backend.app:app", host="0.0.0.0", port=port, reload=True)
