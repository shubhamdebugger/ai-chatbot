import sys
from fakeredis import TcpFakeServer

if __name__ == "__main__":
    print("Starting FakeRedis server on 127.0.0.1:6379...")
    server = TcpFakeServer(("127.0.0.1", 6379))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping FakeRedis server...")
        server.shutdown()
