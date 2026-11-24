#!/usr/bin/env python3
"""Dev helper: drive the dedicated screenshot Chrome (DevTools on 127.0.0.1) with real
input events. Stdlib-only minimal WebSocket client.
Usage: cdp.py PORT URLSUBSTR cmd [args]...   cmds: reload | eval JS | click CSS | type CSS TEXT | wait JS
"""
import base64, json, os, socket, struct, sys, time, urllib.request


class WS:
    def __init__(self, url):
        hostport, path = url[len("ws://"):].split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        self.id = 0

    def _recv(self, n):
        while len(self.rest) < n:
            self.rest += self.s.recv(65536)
        d, self.rest = self.rest[:n], self.rest[n:]
        return d

    def send(self, method, **params):
        self.id += 1
        data = json.dumps({"id": self.id, "method": method, "params": params}).encode()
        hdr = bytes([0x81])
        n = len(data)
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            b1, b2 = self._recv(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            msg = json.loads(self._recv(n))
            if msg.get("id") == self.id:
                return msg.get("result", msg)


def main():
    port, sub, cmd, *args = sys.argv[1:]
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
    tab = next(t for t in tabs if t.get("type") == "page" and sub in t.get("url", ""))
    ws = WS(tab["webSocketDebuggerUrl"])
    ev = lambda js: ws.send("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)["result"].get("value")
    if cmd == "reload":
        ws.send("Page.reload", ignoreCache=True); time.sleep(1.5)
    elif cmd == "eval":
        print(json.dumps(ev(args[0]), ensure_ascii=False))
    elif cmd == "wait":
        for _ in range(300):
            if ev(args[0]):
                print("ok"); return
            time.sleep(0.2)
        sys.exit("wait timeout")
    elif cmd in ("click", "type"):
        r = ev(f"(()=>{{const e=document.querySelector({json.dumps(args[0])});e.scrollIntoView({{block:'center'}});const b=e.getBoundingClientRect();return [b.left+b.width/2,b.top+b.height/2]}})()")
        x, y = r
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            ws.send("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)
        if cmd == "type":
            ws.send("Input.dispatchKeyEvent", type="rawKeyDown", modifiers=4, key="a", code="KeyA", windowsVirtualKeyCode=65, commands=["selectAll"])
            ws.send("Input.dispatchKeyEvent", type="keyUp", modifiers=4, key="a", code="KeyA", windowsVirtualKeyCode=65)
            ws.send("Input.insertText", text=args[1])
        print("clicked", args[0])


if __name__ == "__main__":
    main()
