import os
import sys
import time
import threading
from pyngrok import ngrok, conf

def start_flask():
    """Import and execute the Flask application."""
    from app import app
    # Run Flask in production-friendly mode on port 5000
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

def main():
    port = 5000

    print("=" * 72)
    print("   ORTHOPEDIC RADIOLOGY REGISTRY (ORTHOREG) - CLINICAL PLATFORM   ")
    print("=" * 72)
    print("[1/3] Initializing local database and DICOM ingestion services...")

    # Start Flask in a background daemon thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # Wait 1.5 seconds for Flask to bind port
    time.sleep(1.5)

    print("[2/3] Local Flask Server running on: http://127.0.0.1:5000")
    print("[3/3] Establishing secure remote tunnel via pyngrok...")

    public_url = None
    ngrok_token = os.environ.get('NGROK_AUTHTOKEN', '').strip()

    if ngrok_token:
        try:
            ngrok.set_auth_token(ngrok_token)
        except Exception as e:
            print(f"[Warning] Could not set NGROK_AUTHTOKEN: {e}")

    try:
        # Open an HTTP tunnel on port 5000
        tunnel = ngrok.connect(port)
        public_url = tunnel.public_url
    except Exception as e:
        public_url = None
        error_msg = str(e)
        if "authtoken" in error_msg.lower():
            print("\n[NOTE] ngrok requires a free auth token to create a public URL.")
            print("To enable remote access anywhere from any network:")
            print("1. Sign up for a free account at https://dashboard.ngrok.com/signup")
            print("2. Set your token: set NGROK_AUTHTOKEN=your_token_here")
            print("3. Re-run run.bat")
        else:
            print(f"\n[Warning] pyngrok tunnel failed: {error_msg}")

    print("\n" + "#" * 72)
    print("                   SYSTEM READY & OPERATIONAL")
    print("#" * 72)
    print(f"  • Local Network URL : http://127.0.0.1:{port}")
    if public_url:
        print(f"  • Public Remote URL : {public_url}")
        print("    (Share this URL with the PI to access from anywhere in the world!)")
    else:
        print(f"  • Public Remote URL : Run with NGROK_AUTHTOKEN set for public access")
    print("-" * 72)
    print("  • Default Login Credentials:")
    print("    - Admin / PI : username: admin  | password: admin123  (Full Access)")
    print("    - Viewer     : username: viewer | password: viewer123 (Read-Only)")
    print("#" * 72)
    print("\n[INFO] Press Ctrl+C in this terminal window to terminate server.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Shutting down] Closing tunnels and stopping Flask...")
        try:
            ngrok.kill()
        except Exception:
            pass
        sys.exit(0)

if __name__ == '__main__':
    main()
