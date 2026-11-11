#!/usr/bin/env python3
"""Forward TCP connections from one address to another. Usage: forward.py <listen host> <listen port> <target host> <target port>"""

import socket
import sys
import threading


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main():
    listen_host, listen_port, target_host, target_port = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    server = socket.socket(socket.AF_INET6 if ":" in listen_host else socket.AF_INET)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_host, listen_port))
    server.listen(64)
    print(f"forwarding {listen_host}:{listen_port} to {target_host}:{target_port}", flush=True)

    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection((target_host, target_port))
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    main()
