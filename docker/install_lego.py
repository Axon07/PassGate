"""Download the lego ACME client (used for Let's Encrypt certificates) for
the image's CPU architecture and check it against a pinned SHA-256."""
import hashlib
import io
import os
import sys
import tarfile
import urllib.request

VERSION = "4.25.2"
SHA256 = {
    "amd64": "6022cf99bdc310ebba21c059fcbf1cb5939e17b2f95dade6bb6f878f9590a961",
    "arm64": "3dc4bc343b265a66bb174d3dd03b769bc40c326d680b240b948d12e97ddd4bf8",
}

arch = os.environ.get("TARGETARCH") or {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, "amd64")
dest = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/bin"
url = f"https://github.com/go-acme/lego/releases/download/v{VERSION}/lego_v{VERSION}_linux_{arch}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
digest = hashlib.sha256(data).hexdigest()
if digest != SHA256[arch]:
    sys.exit(f"lego checksum mismatch for {arch}: {digest}")
with tarfile.open(fileobj=io.BytesIO(data)) as t:
    member = t.getmember("lego")
    t.extract(member, dest, filter="data")
os.chmod(os.path.join(dest, "lego"), 0o755)
print(f"installed lego {VERSION} ({arch}) to {dest}")
