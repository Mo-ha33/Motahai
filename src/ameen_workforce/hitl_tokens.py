"""
hitl_tokens.py — Signed, bound, short-lived, single-use approval tokens for Rule D-003.

Rule D-003: Hermes never publishes GTM versions; a human does (or explicitly approves it).
A bare string such as 'SUPERVISOR_APPROVED' is not an approval, because the LLM can type it.
An approval is therefore a token the LLM cannot mint:

    token = base64url(json{cid, wid, exp, nonce}) + "." + base64url(Ed25519 signature over payload_b64)

* Asymmetric:  Ed25519. The ISSUER (Core service, POST /approvals/gtm-publish) holds the PRIVATE key,
               read from the PEM file named by env HITL_SIGNING_PRIVATE_KEY_PATH (unencrypted PKCS8, mode
               0600 on POSIX, owned by the Core service user). The VERIFIER (the MCP server on the Hermes
               host) holds ONLY the PUBLIC key (env HITL_VERIFY_PUBLIC_KEY = base64url raw 32 bytes, or
               HITL_VERIFY_PUBLIC_KEY_PATH = PEM). Hermes runs an LLM with shell tools on that host, so
               anything the MCP server can read, an agent might read: with a shared HMAC secret that
               meant an agent could mint its own approvals; with a public key it cannot.
               Missing/invalid key => fail closed. There is NO HMAC fallback (HITL_SIGNING_KEY is gone).
* Bound:       the token is valid for exactly one container id + one workspace id.
* Short-lived: `exp` (unix seconds), default TTL 30 minutes.
* Single-use:  `nonce` is recorded in a JSON file (env HITL_USED_NONCES_PATH) when it is consumed.

Issuance: POST /approvals/gtm-publish in service.py (operator bearer key only).
Verification: the verifier block below is duplicated INLINE in the deployed consultation.py
(a VPS snapshot that cannot import this package; it needs only `cryptography` installed).
tests/test_hitl_tokens.py asserts the two copies stay byte-for-byte identical, so edit both together.

Key generation (prints only the PUBLIC key; the private key goes to a 0600 file, never to stdout):

    python -m ameen_workforce.hitl_tokens generate-keypair --out /etc/motahai/hitl
"""

import argparse
import base64
import json
import os
import re
import secrets
import sys
import threading
import time

HITL_SIGNING_PRIVATE_KEY_PATH_ENV = "HITL_SIGNING_PRIVATE_KEY_PATH"
HITL_VERIFY_PUBLIC_KEY_ENV = "HITL_VERIFY_PUBLIC_KEY"
HITL_VERIFY_PUBLIC_KEY_PATH_ENV = "HITL_VERIFY_PUBLIC_KEY_PATH"
HITL_USED_NONCES_PATH_ENV = "HITL_USED_NONCES_PATH"
DEFAULT_USED_NONCES_PATH = "/home/deploy/.hermes/hitl_used_nonces.json"
DEFAULT_TTL_SECONDS = 1800
MAX_TTL_SECONDS = 86400
PRIVATE_KEY_FILENAME = "hitl_private.pem"
PUBLIC_KEY_FILENAME = "hitl_public.pem"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --- BEGIN VERIFIER (must stay identical to the copy inlined in consultation.py) ---
_NONCE_LOCK = threading.Lock()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_key():
    """Ed25519 PUBLIC key from env HITL_VERIFY_PUBLIC_KEY (base64url raw 32 bytes) or
    HITL_VERIFY_PUBLIC_KEY_PATH (PEM). None if missing/invalid (fail closed). No private key is ever read here."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        raw = os.environ.get("HITL_VERIFY_PUBLIC_KEY", "").strip()
        if raw:
            return Ed25519PublicKey.from_public_bytes(_b64url_decode(raw))
        path = os.environ.get("HITL_VERIFY_PUBLIC_KEY_PATH", "").strip()
        if path:
            with open(path, "rb") as fh:
                key = load_pem_public_key(fh.read())
            return key if isinstance(key, Ed25519PublicKey) else None
    except Exception:
        return None
    return None


def _consume_nonce(used_nonce_store, nonce: str, exp: int, now: float, record: bool = True) -> bool:
    """Atomically records `nonce` as used. Returns False if it was already used or the store is unusable.
    With record=False it only checks that the nonce is still unused (nothing is written)."""
    with _NONCE_LOCK:
        try:
            if os.path.exists(used_nonce_store):
                with open(used_nonce_store, "r", encoding="utf-8") as fh:
                    used = json.load(fh)
                if not isinstance(used, dict):
                    return False
            else:
                used = {}
            if nonce in used:
                return False
            if not record:
                return True
            used = {n: e for n, e in used.items() if isinstance(e, (int, float)) and e > now}
            used[nonce] = exp
            directory = os.path.dirname(os.path.abspath(used_nonce_store))
            os.makedirs(directory, exist_ok=True)
            tmp_path = f"{used_nonce_store}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(used, fh)
            os.replace(tmp_path, used_nonce_store)
            return True
        except (OSError, ValueError):
            return False


def verify_publish_token(token, container_id, workspace_id, used_nonce_store, now=None, consume=True):
    """Returns (ok, reason). With consume=True (default) an ok token's nonce is spent (single use);
    consume=False validates everything without spending it (dry check before a later consume)."""
    public_key = _verify_key()
    if public_key is None:
        return False, "verify_key_not_configured"
    if not isinstance(token, str) or not token.strip():
        return False, "token_missing"
    now = time.time() if now is None else now
    try:
        from cryptography.exceptions import InvalidSignature
        payload_b64, sig_b64 = token.strip().split(".")
        try:
            public_key.verify(_b64url_decode(sig_b64), payload_b64.encode("ascii"))
        except InvalidSignature:
            return False, "bad_signature"
        claims = json.loads(_b64url_decode(payload_b64))
        cid, wid, exp, nonce = claims["cid"], claims["wid"], claims["exp"], claims["nonce"]
        if not (isinstance(cid, str) and isinstance(wid, str) and isinstance(nonce, str)
                and isinstance(exp, int) and not isinstance(exp, bool) and nonce):
            return False, "token_malformed"
    except (ValueError, KeyError, TypeError):
        return False, "token_malformed"
    if now >= exp:
        return False, "token_expired"
    if cid != container_id:
        return False, "container_mismatch"
    if wid != workspace_id:
        return False, "workspace_mismatch"
    if not _consume_nonce(used_nonce_store, nonce, exp, now, record=consume):
        return False, "nonce_reused_or_store_unavailable"
    return True, "ok"
# --- END VERIFIER ---


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _load_signing_key():
    """Loads the Ed25519 PRIVATE key from env HITL_SIGNING_PRIVATE_KEY_PATH. Raises RuntimeError if the
    path is unset, the file is unreadable/invalid/not Ed25519, or (POSIX) it is group/world accessible."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    path = os.environ.get(HITL_SIGNING_PRIVATE_KEY_PATH_ENV, "").strip()
    if not path:
        raise RuntimeError(f"{HITL_SIGNING_PRIVATE_KEY_PATH_ENV} is not configured; refusing to issue")
    try:
        if os.name == "posix" and os.stat(path).st_mode & 0o077:
            raise RuntimeError(f"{HITL_SIGNING_PRIVATE_KEY_PATH_ENV} file is group/world accessible; chmod 0600")
        with open(path, "rb") as fh:
            key = load_pem_private_key(fh.read(), password=None)
    except RuntimeError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"cannot load signing key from {HITL_SIGNING_PRIVATE_KEY_PATH_ENV}: {type(exc).__name__}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("signing key must be an Ed25519 private key")
    return key


def issue_publish_token(container_id: str, workspace_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """
    Mints an approval token for exactly one (container_id, workspace_id) pair.
    Must only be called on behalf of an authenticated human operator (see service.py).
    Raises ValueError for bad input and RuntimeError if the Ed25519 private key is missing/invalid.
    """
    key = _load_signing_key()
    if not isinstance(container_id, str) or not _ID_PATTERN.match(container_id):
        raise ValueError("container_id must match [A-Za-z0-9_-]{1,64}")
    if not isinstance(workspace_id, str) or not _ID_PATTERN.match(workspace_id):
        raise ValueError("workspace_id must match [A-Za-z0-9_-]{1,64}")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be an integer between 1 and {MAX_TTL_SECONDS}")

    claims = {
        "cid": container_id,
        "wid": workspace_id,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_urlsafe(16),
    }
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = key.sign(payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64url_encode(signature)}"


def used_nonces_path() -> str:
    """Where consumed nonces are recorded (env HITL_USED_NONCES_PATH, VPS default otherwise)."""
    return os.environ.get(HITL_USED_NONCES_PATH_ENV) or DEFAULT_USED_NONCES_PATH


def generate_keypair(out_dir: str) -> str:
    """Writes hitl_private.pem (0600 on POSIX) and hitl_public.pem into out_dir and returns the PUBLIC key as
    base64url raw 32 bytes. Refuses to overwrite existing files. The private key is never returned or printed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    os.makedirs(out_dir, exist_ok=True)
    private_path = os.path.join(out_dir, PRIVATE_KEY_FILENAME)
    public_path = os.path.join(out_dir, PUBLIC_KEY_FILENAME)
    for path in (private_path, public_path):
        if os.path.exists(path):
            raise FileExistsError(f"{path} already exists; refusing to overwrite a key")

    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
    public_key = key.public_key()
    public_pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    raw_public = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(private_pem)
    if os.name == "posix":
        os.chmod(private_path, 0o600)
    with open(public_path, "wb") as fh:
        fh.write(public_pem)
    if os.name == "posix":
        os.chmod(public_path, 0o644)
    return _b64url_encode(raw_public)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ameen_workforce.hitl_tokens")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-keypair", help="create an Ed25519 keypair; prints only the public key")
    gen.add_argument("--out", required=True, help="directory for hitl_private.pem (0600) and hitl_public.pem")
    args = parser.parse_args(argv)
    if args.command == "generate-keypair":
        try:
            public_b64 = generate_keypair(args.out)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(public_b64)
        print(f"private key written to {os.path.join(args.out, PRIVATE_KEY_FILENAME)} (keep it on the Core host only)",
              file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
