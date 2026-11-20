#!/usr/bin/env python3
"""Exercise the Linux snapshot adapter against in-memory streams and private files."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch


worker = runpy.run_path(str(Path(__file__).with_name("transcription-download.py")))
fetch = worker["fetch"]


class Download(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(scratch.directory("model-download-"))
        self.root = Path(temp) / "staging"
        self.root.mkdir()
        self.fd = os.open(self.root, os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.data = b"immutable-model"
        self.manifest = [("sub/model.bin",len(self.data),hashlib.sha256(self.data).hexdigest(),True)]
        self.calls = []

    def stream(self, code=200, content=None, range_header=None):
        @contextmanager
        def read(name, offset):
            self.calls.append((name,offset))
            response = type("Response", (), {"status_code":code,"headers":{"Content-Range":range_header}})()
            yield response, [self.data if content is None else content]
        return read

    def test_warming_the_named_default_only_uses_local_files(self):
        speech=runpy.run_path(str(Path(__file__).with_name("whisper-worker.py")))
        seen={}
        def local(source, **options):
            seen.update(options)
            return source
        with patch.dict(os.environ, {"CLARP_WHISPER_FAKE":"0"}), patch.dict(
                "sys.modules", {"faster_whisper":SimpleNamespace(WhisperModel=local)}):
            self.assertEqual(speech["load"]({"model_source":"small.en"}),"small.en")
        self.assertIs(seen["local_files_only"],True)

    def test_resume_and_validate(self):
        p=self.root/"sub/model.bin"
        p.parent.mkdir()
        p.write_bytes(self.data[:4])
        size=len(self.data)
        fetch(self.fd,size,self.manifest,self.stream(206,self.data[4:],f"bytes 4-{size-1}/{size}"))
        self.assertEqual(p.read_bytes(),self.data)
        self.assertEqual(self.calls,[("sub/model.bin",4)])
        fetch(self.fd,size,self.manifest,self.stream())
        self.assertEqual(len(self.calls),1)
        p.write_bytes(self.data[:2])
        fetch(self.fd,size,self.manifest,self.stream())
        self.assertEqual(p.read_bytes(),self.data)

    def test_smaller_git_blob_and_checksum_failure(self):
        blob=hashlib.sha1(f"blob {len(self.data)}\0".encode()+self.data).hexdigest()
        entry=[("tokens",len(self.data),blob,False)]
        fetch(self.fd,2*len(self.data),entry,self.stream())
        p=self.root/"tokens"
        p.write_bytes(b"x"*len(self.data))
        with self.assertRaisesRegex(ValueError,"checksum"):
            fetch(self.fd,2*len(self.data),entry,self.stream())
        self.assertEqual(p.read_bytes(),b"")

    def test_refuses_without_writing_over_budget_or_wrong_range(self):
        size=len(self.data)
        with self.assertRaisesRegex(ValueError,"catalog byte limit"):
            fetch(self.fd,size-1,self.manifest,self.stream())
        self.assertEqual(self.calls,[])
        with self.assertRaisesRegex(ValueError,"catalog byte limit"):
            fetch(self.fd,size,self.manifest,self.stream(content=self.data+b"x"))
        p=self.root/"sub/model.bin"
        self.assertEqual(p.read_bytes(),b"")
        with self.assertRaisesRegex(ValueError,"incomplete"):
            fetch(self.fd,size,self.manifest,self.stream(content=self.data[:3]))
        self.assertEqual(p.read_bytes(),self.data[:3])
        with self.assertRaisesRegex(ValueError,"range"):
            fetch(self.fd,size,self.manifest,self.stream(206,self.data[3:],"bytes 0-1/2"))
        self.assertEqual(p.read_bytes(),self.data[:3])

    def test_invalid_retained_file_cannot_consume_another_file_budget(self):
        (self.root/"later").write_bytes(b"retained!")
        checksum=hashlib.sha256(b"12345").hexdigest()
        manifest=[(name,5,checksum,True) for name in ("first","later")]
        with self.assertRaisesRegex(ValueError,"staged file exceeds manifest size"):
            fetch(self.fd,10,manifest,self.stream(content=b"12345"))
        self.assertEqual(self.calls,[])
        self.assertFalse((self.root/"first").exists())

    def test_rejects_links_and_escaping_manifest_names(self):
        outside=self.enterContext(scratch.directory("external-model-"))
        keep=Path(outside)/"untouched"
        keep.write_bytes(b"outside")
        dest=self.root/"sub"
        dest.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"unsafe"):
            fetch(self.fd,1024,self.manifest,self.stream())
        dest.unlink()
        os.link(keep,dest)
        with self.assertRaisesRegex(ValueError,"unsafe"):
            fetch(self.fd,1024,self.manifest,self.stream())
        self.assertEqual(keep.read_bytes(),b"outside")
        dest.unlink()
        for name in ("../outside","/absolute","a/../b","a//b","./file"):
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,"invalid snapshot path"):
                fetch(self.fd,1024,[(name,*self.manifest[0][1:])],self.stream())
        self.assertEqual(self.calls,[])


if __name__=="__main__":
    unittest.main()
