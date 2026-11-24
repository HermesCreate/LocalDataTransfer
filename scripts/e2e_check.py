#!/usr/bin/env python3
"""End-to-end check: launches the REAL server process in test mode (fake ssh sandboxes,
never real servers), drives it over HTTP exactly like the browser UI, and verifies files.

Usage: python3 scripts/e2e_check.py [--rsync /usr/bin/rsync]
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time, urllib.error, urllib.request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK = []


def sha_tree(root):
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        sys.exit(1)
    OK.append(label)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rsync")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    work = os.path.join(APP, ".run", "e2e")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    remote = os.path.join(work, "remote")
    subprocess.run([os.path.join(APP, "scripts", "make_sample_data.sh"), "8", "4"], check=True)
    src = os.path.join(APP, "sample_data", "mono-2")
    env = dict(os.environ, DT_SSH_CONFIG=os.path.join(APP, "tests", "fixtures", "ssh_config_demo"),
               DT_SSH_PROGRAM=os.path.join(APP, "tests", "fake_ssh.sh"), FAKE_REMOTE_ROOT=remote,
               DT_CONFIG_PATH=os.path.join(work, "config.json"))
    if a.rsync:
        env["DT_RSYNC"] = a.rsync
    url_file = os.path.join(work, "url")
    srv = subprocess.Popen([sys.executable, os.path.join(APP, "transfer_tool.py"), "--no-browser", "--port", str(a.port),
                            "--url-file", url_file], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            if os.path.exists(url_file) and open(url_file).read().strip():
                break
            time.sleep(0.1)
        base = open(url_file).read().strip().rstrip("/")

        def req(path, body=None):
            r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=10) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        def wait():
            for _ in range(1200):
                s = req("/api/status")[1]
                if not s["running"]:
                    return s
                time.sleep(0.1)
            raise SystemExit("timeout")

        check(req("/api/health")[1]["version"] == "3", f"server up at {base}, /api/health version 3")
        hosts = req("/api/hosts")[1]["hosts"]
        check(hosts == ["demo-11.20", "demo-H800-1", "demo-H800-2", "bad-offline"], f"hosts from demo ssh config: {hosts}")
        servers = ["demo-11.20", "demo-H800-1", "demo-H800-2"]
        for h in servers:
            os.makedirs(os.path.join(remote, h, "datasets"), exist_ok=True)
        req("/api/start", {"local": src, "servers": servers, "remote_path": "datasets/mono-2", "direction": "local_to_server"})
        s = wait()
        check(s["status"] == "完成" and s["percent"] == 100, f"upload to 3 servers: {s['status']} {s['percent']}%")
        for h in servers:
            check(sha_tree(os.path.join(remote, h, "datasets/mono-2")) == sha_tree(src), f"{h}: uploaded tree matches (sha256)")
        stats = [l for l in s["logs"] if "Number of regular files transferred" in l or "Number of files transferred" in l]
        check(len(stats) == 3, "rsync --stats output present in logs for each server")
        dl = os.path.join(work, "download")
        req("/api/start", {"local": dl, "servers": servers, "remote_path": "datasets/mono-2/", "direction": "server_to_local"})
        s = wait()
        check(s["status"] == "完成", "download from 3 servers: 完成")
        for h in servers:
            check(sha_tree(os.path.join(dl, h)) == sha_tree(src), f"download isolated into {os.path.relpath(os.path.join(dl, h), APP)}")
        req("/api/start", {"local": src, "servers": ["bad-offline", "demo-11.20"], "remote_path": "datasets/mono-2",
                           "direction": "local_to_server"})
        s = wait()
        check(s["status"] == "部分失败" and [r["status"] for r in s["results"]] == ["失败", "完成"],
              f"bad-offline fails, next server still runs -> {s['status']}")
        cfg = json.load(open(env["DT_CONFIG_PATH"]))
        check(cfg["servers"] == ["bad-offline", "demo-11.20"] and cfg["direction"] == "local_to_server", "config persisted on start")
        print(f"\nE2E OK: {len(OK)} checks passed")
    finally:
        srv.terminate()
        srv.wait(5)


if __name__ == "__main__":
    main()
