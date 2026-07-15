import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server import SSHClient


class FakeSFTP:
    def __init__(self):
        self.directories = {"/", "/remote"}
        self.files = {}
        self.put_calls = []
        self.closed = False

    @staticmethod
    def _normalize(path):
        normalized = path.replace("\\", "/").rstrip("/")
        return normalized or "/"

    def stat(self, path):
        path = self._normalize(path)
        if path in self.directories:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0, st_mtime=0)
        if path in self.files:
            values = self.files[path]
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_size=values["size"],
                st_mtime=values["mtime"],
            )
        raise OSError(path)

    def mkdir(self, path):
        self.directories.add(self._normalize(path))

    def listdir_attr(self, path):
        path = self._normalize(path)
        prefix = "/" if path == "/" else path + "/"
        entries = {}
        for directory in self.directories:
            if directory == path or not directory.startswith(prefix):
                continue
            relative = directory[len(prefix):]
            if "/" not in relative:
                entries[relative] = SimpleNamespace(
                    filename=relative,
                    st_mode=stat.S_IFDIR | 0o755,
                    st_size=0,
                    st_mtime=0,
                )
        for file_path, values in self.files.items():
            if not file_path.startswith(prefix):
                continue
            relative = file_path[len(prefix):]
            if "/" not in relative:
                entries[relative] = SimpleNamespace(
                    filename=relative,
                    st_mode=stat.S_IFREG | 0o644,
                    st_size=values["size"],
                    st_mtime=values["mtime"],
                )
        return list(entries.values())

    def put(self, local_path, remote_path, confirm=True):
        remote_path = self._normalize(remote_path)
        self.put_calls.append((Path(local_path).name, remote_path, confirm))
        self.files[remote_path] = {
            "size": os.path.getsize(local_path),
            "mtime": int(os.path.getmtime(local_path)),
        }

    def utime(self, remote_path, times):
        self.files[self._normalize(remote_path)]["mtime"] = int(times[1])

    def close(self):
        self.closed = True


class FakeParamikoClient:
    def __init__(self, sftp):
        self.sftp = sftp

    def open_sftp(self):
        return self.sftp


class IncrementalSFTPSyncTests(unittest.TestCase):
    def test_exclude_rules_use_exact_names_and_globs(self):
        rules = ["venv", "build", "*.pyc", ".env"]
        self.assertTrue(SSHClient._should_exclude_upload_entry("venv", 0, rules))
        self.assertTrue(SSHClient._should_exclude_upload_entry("build", 2, rules))
        self.assertTrue(SSHClient._should_exclude_upload_entry("cache.pyc", 2, rules))
        self.assertTrue(SSHClient._should_exclude_upload_entry(".env", 0, rules))
        self.assertFalse(SSHClient._should_exclude_upload_entry("build_index.py", 2, rules))
        self.assertFalse(SSHClient._should_exclude_upload_entry(".env", 1, rules))

    def test_upload_skips_unchanged_files_and_preserves_mtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            same = root / "same.txt"
            changed = root / "changed.txt"
            build_index = root / "build_index.py"
            ignored_venv_file = root / "venv" / "ignored.py"
            ignored_build_file = root / "build" / "asset.js"

            same.write_bytes(b"same")
            changed.write_bytes(b"new content")
            build_index.write_bytes(b"script")
            ignored_venv_file.parent.mkdir()
            ignored_venv_file.write_bytes(b"ignored")
            ignored_build_file.parent.mkdir()
            ignored_build_file.write_bytes(b"ignored")
            os.utime(same, (1000, 1000))
            os.utime(changed, (2000, 2000))

            sftp = FakeSFTP()
            sftp.files["/remote/same.txt"] = {"size": 4, "mtime": 1500}
            # 大小相同但本地修改时间更新时也必须重新上传。
            sftp.files["/remote/changed.txt"] = {"size": len(b"new content"), "mtime": 1500}
            ssh = SSHClient("example", 22, "user")
            ssh.client = FakeParamikoClient(sftp)

            success, first_stats = ssh.upload_dir(
                str(root),
                "/remote",
                excludes=["venv", "build"],
            )

            self.assertTrue(success)
            self.assertEqual(first_stats["checked"], 3)
            self.assertEqual(first_stats["uploaded"], 2)
            self.assertEqual(first_stats["skipped"], 1)
            self.assertEqual(first_stats["excluded"], 2)
            self.assertEqual(
                {name for name, _, _ in sftp.put_calls},
                {"changed.txt", "build_index.py"},
            )
            self.assertTrue(all(confirm is False for _, _, confirm in sftp.put_calls))
            self.assertEqual(
                sftp.files["/remote/changed.txt"]["mtime"],
                int(changed.stat().st_mtime),
            )
            self.assertTrue(sftp.closed)

            sftp.put_calls.clear()
            sftp.closed = False
            success, second_stats = ssh.upload_dir(
                str(root),
                "/remote",
                excludes=["venv", "build"],
            )

            self.assertTrue(success)
            self.assertEqual(second_stats["uploaded"], 0)
            self.assertEqual(second_stats["skipped"], 3)
            self.assertEqual(sftp.put_calls, [])
            self.assertTrue(sftp.closed)


if __name__ == "__main__":
    unittest.main()
