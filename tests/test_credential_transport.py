import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sdd_data.auth import CookieManager
from sdd_data.config import Settings
from sdd_data.credential_transport import CredentialTransport


class CredentialTransportTests(unittest.TestCase):
    def test_roundtrip_restart_worker_binding_and_tampering(self):
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.credential_transport._crypt', side_effect=lambda value, **kwargs: value):
            root = Path(folder)
            manager = CookieManager(Settings(root, root, root/'config/cookies/spx.json', root/'sa', '', ''))
            transport = CredentialTransport(manager)
            restored = CredentialTransport(manager)
            self.assertEqual(transport.key_id, restored.key_id)
            key, iv = AESGCM.generate_key(256), os.urandom(12)
            encode = lambda value: base64.b64encode(value).decode()
            envelope = dict(version=1, keyId=transport.key_id, iv=encode(iv),
                key=encode(transport.key.public_key().encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))),
                data=encode(AESGCM(key).encrypt(iv, json.dumps(dict(username='test-user', password='dummy-password')).encode(), f'worker:{transport.key_id}'.encode())))
            self.assertEqual(restored.decrypt(envelope, 'worker'), ('test-user', 'dummy-password'))
            for invalid, worker in [(envelope, 'wrong-worker'), ({**envelope, 'keyId':'wrong'}, 'worker'), ({**envelope, 'data':encode(b'tampered')}, 'worker')]:
                with self.assertRaises(Exception):
                    restored.decrypt(invalid, worker)

    @unittest.skipUnless(os.environ.get('NODE_EXECUTABLE'), 'Cross-runtime test needs Node executable')
    def test_real_browser_envelope_decrypts_in_python(self):
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.credential_transport._crypt', side_effect=lambda value, **kwargs: value):
            root = Path(folder)
            manager = CookieManager(Settings(root, root, root/'config/cookies/spx.json', root/'sa', '', ''))
            transport = CredentialTransport(manager)
            module = (Path(__file__).resolve().parents[2]/'sdd-performance-project/lib/data-sources/spx-credentials.ts').as_uri()
            script = f"import {{encryptSpxCredentials}} from {json.dumps(module)}; let raw=''; for await (const x of process.stdin) raw+=x; const p=JSON.parse(raw); console.log(JSON.stringify(await encryptSpxCredentials('worker',p.key,p.id,'test-user','dummy-password')));"
            result = subprocess.run([os.environ['NODE_EXECUTABLE'], '--experimental-strip-types', '--input-type=module', '-e', script],
                input=json.dumps(dict(key=transport.public_key, id=transport.key_id)), capture_output=True, text=True, check=True)
            self.assertEqual(transport.decrypt(json.loads(result.stdout), 'worker'), ('test-user', 'dummy-password'))
