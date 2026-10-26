"""Worker-only RSA key; encrypted credential envelopes are bound to worker and key ID."""
import base64
import hashlib
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import _crypt, atomic_write


class CredentialTransport:
    def __init__(self, manager):
        path = manager.state / 'web-transport-key.dat'
        with manager.locked():
            if path.is_file():
                private = _crypt(base64.b64decode(path.read_bytes()), decrypt=True)
                self.key = serialization.load_pem_private_key(private, password=None)
            else:
                self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                private = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                atomic_write(path, base64.b64encode(_crypt(private)))
        public = self.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.public_key = public.decode('ascii')
        self.key_id = hashlib.sha256(public).hexdigest()

    def decrypt(self, envelope, worker):
        if not isinstance(envelope, dict) or envelope.get('version') != 1 or envelope.get('keyId') != self.key_id:
            raise ValueError('Invalid credential envelope')
        decode = lambda name: base64.b64decode(envelope[name], validate=True)
        key = self.key.decrypt(decode('key'), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        raw = AESGCM(key).decrypt(decode('iv'), decode('data'), f'{worker}:{self.key_id}'.encode())
        value = json.loads(raw)
        username, password = value.get('username'), value.get('password')
        if not isinstance(username, str) or not username.strip() or len(username) > 256 or not isinstance(password, str) or not password or len(password) > 1024:
            raise ValueError('Invalid credentials')
        return username.strip(), password
