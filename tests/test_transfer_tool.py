"""Tests for Data Transfer (PRD acceptance criteria). Run: python3 -m unittest discover -s tests -v

Integration tests use tests/fake_ssh.sh: each fake host is a sandbox folder, so real rsync runs
end-to-end but no real server and no ~/.ssh file is ever touched.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)
import transfer_tool as tt  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
FAKE_SSH = os.path.join(HERE, "fake_ssh.sh")
RSYNC = tt.find_rsync()


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            out[os.path.relpath(p, root)] = sha(p)
    return out


class TestSSHConfig(unittest.TestCase):
    def test_hosts_order_include_wildcards_dedupe(self):
        hosts = tt.parse_ssh_hosts(os.path.join(FIX, "ssh_config"))
        self.assertEqual(hosts, ["included-a", "included-b", "11.20", "H800-1", "H800-2", "jump"])

    def test_no_hostname_user_or_comment_leak(self):
        hosts = tt.parse_ssh_hosts(os.path.join(FIX, "ssh_config"))
        for bad in ["10.0.11.20", "keal_yue", "*", "gpu-??", "!blocked", "duplicate,", "192.168.1.2"]:
            self.assertNotIn(bad, hosts)

    def test_missing_file_returns_empty(self):
        self.assertEqual(tt.parse_ssh_hosts("/nonexistent/ssh_config"), [])

    def test_absolute_include_and_loop(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            open(a, "w").write(f"Host one\nInclude {b}\nHost two\n")
            open(b, "w").write(f"Host three\nInclude {a}\n")
            self.assertEqual(tt.parse_ssh_hosts(a), ["one", "three", "two"])


class TestValidation(unittest.TestCase):
    def test_remote_path_rules(self):
        ok = ["/data/video", "~/DataSets/x", "relative/dir", "/home/u/a:b"]
        for p in ok:
            self.assertEqual(tt.validate_remote_path(p, ["11.20"]), p)
        for p in ["", "   ", "11.20:/data", "user@host:/data", "ssh://host/data", "H800-1:/x", "u@h/x", "host:/x"]:
            with self.assertRaises(tt.UserError, msg=p):
                tt.validate_remote_path(p, ["11.20", "H800-1"])

    def test_start_validation(self):
        with tempfile.TemporaryDirectory() as d:
            base = {"local": d, "servers": ["11.20"], "remote_path": "/data", "direction": "local_to_server"}
            self.assertEqual(tt.validate_start(base, ["11.20"])["local"], os.path.abspath(d))
            cases = [({"servers": []}, "至少选择"), ({"remote_path": ""}, "远程路径不能为空"),
                     ({"local": os.path.join(d, "nope")}, "不存在"), ({"servers": ["evil"]}, "~/.ssh/config"),
                     ({"direction": "x"}, "方向"), ({"remote_path": "11.20:/data"}, "别名")]
            for patch, needle in cases:
                with self.assertRaises(tt.UserError) as cm:
                    tt.validate_start({**base, **patch}, ["11.20"])
                self.assertIn(needle, str(cm.exception))
            # download: missing local target is allowed (created later)
            spec = tt.validate_start({**base, "local": os.path.join(d, "new"), "direction": "server_to_local"}, ["11.20"])
            self.assertTrue(spec["local"].endswith("new"))

    def test_normalize_local(self):
        os.environ["DT_TEST_VAR"] = "/tmp/xyz"
        self.assertEqual(tt.normalize_local("  $DT_TEST_VAR/a "), "/tmp/xyz/a")
        self.assertEqual(tt.normalize_local("~/foo"), os.path.join(os.path.expanduser("~"), "foo"))
        self.assertTrue(os.path.isabs(tt.normalize_local("rel/p")))


class TestRsyncCommand(unittest.TestCase):
    def test_prd_args(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DT_SSH_PROGRAM", None)
            args = tt.build_rsync_args("rsync", "SRC", "DST")
        self.assertEqual(args, ["rsync", "-rlt", "--no-owner", "--no-group", "--no-perms", "--whole-file", "--partial",
                                "--info=progress2", "--human-readable", "--stats", "-e", "ssh -T -o Compression=no", "SRC", "DST"])

    def test_openrsync_fallback_args(self):
        args = tt.build_rsync_args("/usr/bin/rsync", "S", "D", progress2=False)
        self.assertIn("--partial", args)
        self.assertNotIn("--info=progress2", args)

    def test_plan_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            up = tt.plan_jobs({"local": d, "servers": ["11.20", "H800-1"], "remote_path": "/data/v", "direction": "local_to_server"})
            self.assertEqual([j["dest"] for j in up], ["11.20:/data/v", "H800-1:/data/v"])
            self.assertEqual(up[0]["source"], d + "/")
            one = tt.plan_jobs({"local": d, "servers": ["11.20"], "remote_path": "/data/v", "direction": "server_to_local"})
            self.assertEqual((one[0]["source"], one[0]["dest"]), ("11.20:/data/v", d + "/"))
            multi = tt.plan_jobs({"local": d, "servers": ["11.20", "H800-1", "H800-2"], "remote_path": "/r", "direction": "server_to_local"})
            self.assertEqual([j["dest"] for j in multi], [os.path.join(d, h) + "/" for h in ["11.20", "H800-1", "H800-2"]])

    def test_parse_progress(self):
        p = tt.parse_progress("      1.05G  45%   12.34MB/s    0:01:23 (xfr#3, to-chk=2/10)")
        self.assertEqual((p["percent"], p["speed"], p["eta"]), (45, "12.34MB/s", "0:01:23"))
        p = tt.parse_progress("  2,621,440  68%    2.14MB/s    0:04:30")
        self.assertEqual((p["percent"], p["speed"]), (68, "2.14MB/s"))
        self.assertEqual(tt.fmt_eta("04:30"), "0:04:30")
        self.assertIsNone(tt.parse_progress("sending incremental file list"))
        self.assertIsNone(tt.parse_progress("Number of files: 12"))


class TestConfigAndChooser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"DT_CONFIG_PATH": os.path.join(self.tmp, "cfg.json")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def test_roundtrip(self):
        self.assertEqual(tt.load_config()["servers"], [])
        tt.save_config({"local": "/a"})
        tt.save_config({"servers": ["11.20"], "remote_path": "/r", "direction": "server_to_local"})
        self.assertEqual(tt.load_config(), {"local": "/a", "servers": ["11.20"], "remote_path": "/r", "direction": "server_to_local"})
        with open(os.environ["DT_CONFIG_PATH"], encoding="utf-8") as fh:
            json.load(fh)

    def test_write_failure_is_readable(self):
        os.environ["DT_CONFIG_PATH"] = os.path.join(self.tmp, "missing-dir", "cfg.json")
        with self.assertRaises(tt.UserError) as cm:
            tt.save_config({"local": "/a"})
        self.assertIn("写入配置文件失败", str(cm.exception))

    def test_corrupt_read_is_readable(self):
        open(os.environ["DT_CONFIG_PATH"], "w").write("{bad json")
        with self.assertRaises(tt.UserError) as cm:
            tt.load_config()
        self.assertIn("读取配置文件失败", str(cm.exception))

    @unittest.skipUnless(sys.platform == "darwin", "macOS chooser")
    def test_chooser_success_cancel_error(self):
        ok = subprocess.CompletedProcess([], 0, stdout="/Users/x/Data/\n", stderr="")
        with mock.patch("subprocess.run", return_value=ok) as run:
            self.assertEqual(tt.choose_folder(), {"ok": True, "path": "/Users/x/Data/", "cancelled": False})
            self.assertEqual(run.call_args[0][0][0], "/usr/bin/osascript")
            self.assertIn("choose folder", " ".join(run.call_args[0][0]))
        cancel = subprocess.CompletedProcess([], 1, stdout="", stderr="execution error: User canceled. (-128)")
        with mock.patch("subprocess.run", return_value=cancel):
            self.assertEqual(tt.choose_folder(), {"ok": True, "path": None, "cancelled": True})
        err = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=err), self.assertRaises(tt.UserError):
            tt.choose_folder()
        with mock.patch("os.path.exists", return_value=False), self.assertRaises(tt.UserError) as cm:
            tt.choose_folder()
        self.assertIn("找不到系统文件夹选择器", str(cm.exception))

    def test_linux_no_chooser(self):
        with mock.patch.object(tt.sys, "platform", "linux"), mock.patch("shutil.which", return_value=None):
            with self.assertRaises(tt.UserError) as cm:
                tt.choose_folder()
        self.assertIn("找不到可用的系统文件夹选择器", str(cm.exception))


@unittest.skipUnless(RSYNC, "rsync not installed")
class TestHTTPAndTransfers(unittest.TestCase):
    """Backend + real rsync through fake ssh sandboxes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dt-test-")
        cls.remote_root = os.path.join(cls.tmp, "remote")
        cls.ssh_cfg = os.path.join(cls.tmp, "ssh_config")
        open(cls.ssh_cfg, "w").write("Host *\nHost srv-a\nHost srv-b srv-c\nHost bad-x\n")
        cls.env = mock.patch.dict(os.environ, {
            "DT_SSH_CONFIG": cls.ssh_cfg, "DT_SSH_PROGRAM": FAKE_SSH, "FAKE_REMOTE_ROOT": cls.remote_root,
            "DT_CONFIG_PATH": os.path.join(cls.tmp, "cfg.json")})
        cls.env.start()
        tt.MANAGER = tt.TransferManager()
        cls.httpd = tt.make_server(18765)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # sample data
        cls.src = os.path.join(cls.tmp, "mono-2")
        os.makedirs(os.path.join(cls.src, "videos"))
        for i in range(3):
            with open(os.path.join(cls.src, "videos", f"clip{i}.mp4"), "wb") as fh:
                fh.write(os.urandom(512 * 1024 + i))
        open(os.path.join(cls.src, "meta.json"), "w").write('{"ok": true}')

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.wait_idle()
        os.environ.pop("DT_EXTRA_RSYNC_ARGS", None)

    # helpers
    def req(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def wait_idle(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _, s = self.req("/api/status")
            if not s["running"]:
                return s
            time.sleep(0.1)
        self.fail("transfer did not finish")

    def mk_remote(self, host, rel):
        os.makedirs(os.path.join(self.remote_root, host, rel), exist_ok=True)

    # tests
    def test_get_endpoints(self):
        code, h = self.req("/api/health")
        self.assertEqual((code, h["ok"], h["version"]), (200, True, "3"))
        self.assertEqual(self.req("/api/hosts")[1], {"hosts": ["srv-a", "srv-b", "srv-c", "bad-x"]})
        s = self.req("/api/status")[1]
        for k in ["running", "status", "percent", "speed", "eta", "direction", "local", "servers", "remote_path",
                  "current_server", "return_code", "results", "logs"]:
            self.assertIn(k, s)
        self.assertIn("local", self.req("/api/config")[1])
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            html = r.read().decode()
        for text in ["Data Transfer", "LOCAL TOOL / RSYNC + SSH", 'aria-label="打开文件管理器"', "刷新 config",
                     "本地 → 服务器", "服务器 → 本地", "开始传输", "停止传输", "传输日志", "工具不会保存密码"]:
            self.assertIn(text, html)
        code, _ = self.req("/../transfer_tool.py")
        self.assertEqual(code, 404)

    def test_start_errors_are_chinese_json(self):
        cases = [({"servers": []}, "至少选择"), ({"remote_path": ""}, "远程路径不能为空"),
                 ({"remote_path": "srv-a:/x"}, "别名"), ({"local": "/definitely/not/here"}, "不存在"),
                 ({"servers": ["not-in-config"]}, "~/.ssh/config")]
        for patch, needle in cases:
            body = {"local": self.src, "servers": ["srv-a"], "remote_path": "up", "direction": "local_to_server", **patch}
            code, r = self.req("/api/start", body)
            self.assertEqual(code, 400, patch)
            self.assertFalse(r["ok"])
            self.assertIn(needle, r["error"])
            self.assertNotIn("Traceback", r["error"])

    def test_stop_when_idle(self):
        code, r = self.req("/api/stop", {})
        self.assertEqual((code, r["ok"], r["status"]["running"]), (200, True, False))

    def test_upload_multi_server_and_config_saved(self):
        for h in ["srv-a", "srv-b"]:
            self.mk_remote(h, "datasets")
        code, r = self.req("/api/start", {"local": self.src, "servers": ["srv-a", "srv-b"],
                                          "remote_path": "datasets/mono-2", "direction": "local_to_server"})
        self.assertEqual(code, 200, r)
        s = self.wait_idle()
        self.assertEqual((s["status"], s["percent"], s["return_code"]), ("完成", 100, 0), s["logs"][-5:])
        for h in ["srv-a", "srv-b"]:
            self.assertEqual(tree(os.path.join(self.remote_root, h, "datasets/mono-2")), tree(self.src))
        log = "\n".join(s["logs"])
        self.assertIn("[srv-a] 开始", log)
        self.assertIn("[srv-b] 完成", log)
        self.assertLess(log.index("[srv-a] 开始"), log.index("[srv-b] 开始"))  # serial, in selection order
        self.assertIn("srv-a:datasets/mono-2", log)
        cfg = self.req("/api/config")[1]
        self.assertEqual(cfg, {"local": self.src, "servers": ["srv-a", "srv-b"], "remote_path": "datasets/mono-2",
                               "direction": "local_to_server"})

    def test_download_single_and_multi(self):
        for h in ["srv-a", "srv-b", "srv-c"]:
            d = os.path.join(self.remote_root, h, "export")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "from.txt"), "w").write(h)
        single = os.path.join(self.tmp, "dl-single")
        self.req("/api/start", {"local": single, "servers": ["srv-a"], "remote_path": "export/", "direction": "server_to_local"})
        self.assertEqual(self.wait_idle()["status"], "完成")
        self.assertEqual(open(os.path.join(single, "from.txt")).read(), "srv-a")
        multi = os.path.join(self.tmp, "dl-multi")
        self.req("/api/start", {"local": multi, "servers": ["srv-a", "srv-b", "srv-c"], "remote_path": "export/",
                                "direction": "server_to_local"})
        self.assertEqual(self.wait_idle()["status"], "完成")
        for h in ["srv-a", "srv-b", "srv-c"]:
            self.assertEqual(open(os.path.join(multi, h, "from.txt")).read(), h)  # isolated per alias

    def test_partial_and_total_failure(self):
        self.mk_remote("srv-c", "pf")
        self.req("/api/start", {"local": self.src, "servers": ["bad-x", "srv-c"], "remote_path": "pf/mono-2",
                                "direction": "local_to_server"})
        s = self.wait_idle()
        self.assertEqual(s["status"], "部分失败")
        self.assertEqual([r["status"] for r in s["results"]], ["失败", "完成"])
        self.assertNotEqual(s["results"][0]["return_code"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.remote_root, "srv-c", "pf/mono-2")))  # continued after failure
        self.req("/api/start", {"local": self.src, "servers": ["bad-x"], "remote_path": "pf", "direction": "local_to_server"})
        self.assertEqual(self.wait_idle()["status"], "失败")

    def test_progress_stop_and_resume(self):
        big = os.path.join(self.tmp, "big")
        os.makedirs(big, exist_ok=True)
        for i in range(4):
            with open(os.path.join(big, f"v{i}.bin"), "wb") as fh:
                fh.write(os.urandom(2 * 1024 * 1024))
        self.mk_remote("srv-a", "resume")
        os.environ["DT_EXTRA_RSYNC_ARGS"] = "--bwlimit=1500k"
        body = {"local": big, "servers": ["srv-a"], "remote_path": "resume/big", "direction": "local_to_server"}
        self.assertEqual(self.req("/api/start", body)[0], 200)
        # second start while running is rejected
        code, r = self.req("/api/start", body)
        self.assertEqual(code, 400)
        self.assertIn("正在进行", r["error"])
        seen = None
        end = time.time() + 20
        while time.time() < end:
            s = self.req("/api/status")[1]
            if s["percent"] > 0 and s["speed"] != "0 B/s" and s["eta"] != "--":
                seen = s
                break
            time.sleep(0.2)
        self.assertIsNotNone(seen, "no live progress/speed/ETA reported")
        self.assertEqual(seen["current_server"], "srv-a")
        time.sleep(1.5)
        code, r = self.req("/api/stop", {})
        self.assertEqual(code, 200)
        self.assertIn(r["status"]["status"], ("正在停止", "已停止"))
        s = self.wait_idle()
        self.assertEqual(s["status"], "已停止")
        dest = os.path.join(self.remote_root, "srv-a", "resume/big")
        kept = sum(os.path.getsize(os.path.join(dest, f)) for f in os.listdir(dest)) if os.path.isdir(dest) else 0
        self.assertGreater(kept, 0, "partial data should be kept after stop")
        self.assertLess(kept, 8 * 1024 * 1024)
        os.environ.pop("DT_EXTRA_RSYNC_ARGS")
        self.req("/api/start", body)
        s = self.wait_idle()
        self.assertEqual(s["status"], "完成")
        self.assertEqual(tree(dest), tree(big))


if __name__ == "__main__":
    unittest.main(verbosity=2)
