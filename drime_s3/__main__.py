"""CLI entrypoint for Drime S3 Gateway."""

import argparse
import logging
import sys

from cheroot.wsgi import Server as WSGIServer

from .api import DrimeClient
from .gateway import S3Gateway


def main():
    parser = argparse.ArgumentParser(description="Drime S3 Gateway")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind (default: 8081)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    try:
        client = DrimeClient()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Set DRIME_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)
    
    app = S3Gateway(client)
    # Increase thread pool to support high concurrency (e.g. restic -o s3.connections=32)
    server = WSGIServer((args.host, args.port), app, numthreads=100)
    
    print(f"Drime S3 Gateway running on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop")
    
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
        print("\nShutdown complete")


if __name__ == "__main__":
    main()
