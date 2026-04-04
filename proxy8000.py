import socket, threading, os

def forward(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except:
        pass
    finally:
        try: src.close()
        except: pass
        try: dst.close()
        except: pass

def handle(client):
    try:
        backend = socket.create_connection(('127.0.0.1', 8001), timeout=5)
        t1 = threading.Thread(target=forward, args=(client, backend), daemon=True)
        t2 = threading.Thread(target=forward, args=(backend, client), daemon=True)
        t1.start(); t2.start()
    except Exception as e:
        try: client.close()
        except: pass

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', 8000))
srv.listen(50)
print("Proxy 8000->8001 started", flush=True)

with open('C:/Users/Charles/Desktop/Projet Claude/Alarm2.0/proxy8000.pid', 'w') as f:
    f.write(str(os.getpid()))

while True:
    try:
        client, addr = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()
    except Exception as e:
        print(f"Error: {e}", flush=True)
        break
