#!/usr/bin/env python3
"""crypto_helper.py — 加解密 / 签名 / 编码 多合一工具（Sharp worker 内置）。

场景：渗透时目标请求体被 AES/DES/3DES/SM4 加密、参数带 MD5/HMAC/SM3 签名，
Burp 抓到的全是密文改不动包。已知 key/IV（从 JS、APP、配置里逆出来）时，
用本工具解密密文看明文、篡改后重新加密发包。

依赖：pycryptodome（AES/DES/3DES/RSA/hash）、gmssl（国密 SM2/SM3/SM4）。
      两者已装进镜像；缺失时对应功能会给出明确报错。

设计原则（区别于 GUI 逆向工具的“便利默认”）：
  - key/IV 必须显式给，长度不对直接报错，绝不静默截断或用 key 前 N 字节当 IV。
  - 对称加密默认输出 base64，解密默认按 UTF-8 尝试解码、失败回退 hex。

CLI 速查：
  python3 crypto_helper.py aes  --op dec --mode cbc --key-hex .. --iv-hex .. --in-b64 ..
  python3 crypto_helper.py aes  --op enc --mode gcm --key-utf8 .. --iv-hex .. --in-utf8 ..
  python3 crypto_helper.py sm4  --op enc --mode cbc --key-utf8 .. --iv-hex .. --in-utf8 ..
  python3 crypto_helper.py hash --alg sm3 --in-utf8 ..
  python3 crypto_helper.py hmac --alg sha256 --key-utf8 .. --in-utf8 ..
  python3 crypto_helper.py enc  --op b64d --in ..
  python3 crypto_helper.py jwt  --token eyJ...

导入用法：
  import sys; sys.path.insert(0, '/home/kali/tools')
  from crypto_helper import aes_decrypt, sm4_encrypt
"""
from __future__ import annotations
import base64
import binascii
import hashlib
import hmac as _hmac
import json
import sys

# --- 输入物料解析：把 hex/base64/utf8 统一成 bytes，长度错误明确报错 -------------

def material(*, hexv=None, b64=None, utf8=None, raw=None) -> bytes:
    """从 --*-hex / --*-b64 / --*-utf8 里取一个，转成 bytes。"""
    given = [(k, v) for k, v in (("hex", hexv), ("b64", b64), ("utf8", utf8)) if v is not None]
    if raw is not None:
        return raw
    if len(given) != 1:
        raise ValueError("需要且只能提供一种编码来源（hex / b64 / utf8）")
    kind, val = given[0]
    if kind == "hex":
        return binascii.unhexlify(val.strip().replace(" ", ""))
    if kind == "b64":
        return base64.b64decode(val.strip())
    return val.encode("utf-8")


def _decode_out(b: bytes) -> str:
    """解密结果优先按 UTF-8 显示，失败回退 hex。"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return "hex:" + b.hex()


# --- AES / DES / 3DES（pycryptodome）------------------------------------------

def _blockcipher(algo, key: bytes, mode: str, iv: bytes | None,
                 data: bytes, op: str) -> bytes:
    from Crypto.Cipher import AES, DES, DES3  # noqa
    from Crypto.Util.Padding import pad, unpad
    mods = {"AES": AES, "DES": DES, "DES3": DES3}
    C = mods[algo]
    mode = mode.lower()
    block = 16 if algo == "AES" else 8
    mode_map = {
        "ecb": C.MODE_ECB, "cbc": C.MODE_CBC, "cfb": C.MODE_CFB,
        "ofb": C.MODE_OFB, "ctr": C.MODE_CTR, "gcm": C.MODE_GCM,
    }
    if mode not in mode_map:
        raise ValueError(f"不支持的模式: {mode}")
    kwargs = {}
    if mode not in ("ecb", "ctr"):
        if iv is None:
            raise ValueError(f"{mode.upper()} 模式必须提供 --iv-*")
        kwargs["iv" if mode != "gcm" else "nonce"] = iv
    if mode == "ctr":
        if iv is None:
            raise ValueError("CTR 模式必须提供 --iv-* 作为 nonce/初始计数器")
        from Crypto.Util import Counter
        kwargs["counter"] = Counter.new(block * 8,
                                        initial_value=int.from_bytes(iv, "big"))
    cipher = C.new(key, mode_map[mode], **kwargs)
    if op == "enc":
        if mode in ("ecb", "cbc"):
            data = pad(data, block)
        return cipher.encrypt(data)
    out = cipher.decrypt(data)
    if mode in ("ecb", "cbc"):
        out = unpad(out, block)
    return out


def aes_encrypt(key, data, mode="cbc", iv=None):
    return _blockcipher("AES", key, mode, iv, data, "enc")


def aes_decrypt(key, data, mode="cbc", iv=None):
    return _blockcipher("AES", key, mode, iv, data, "dec")


def sm4_encrypt(key: bytes, data: bytes, mode="cbc", iv=None) -> bytes:
    from gmssl.sm4 import CryptSM4, SM4_ENCRYPT
    c = CryptSM4()
    c.set_key(key, SM4_ENCRYPT)
    if mode.lower() == "cbc":
        if iv is None:
            raise ValueError("SM4-CBC 必须提供 --iv-*")
        return c.crypt_cbc(iv, data)
    return c.crypt_ecb(data)


def sm4_decrypt(key: bytes, data: bytes, mode="cbc", iv=None) -> bytes:
    from gmssl.sm4 import CryptSM4, SM4_DECRYPT
    c = CryptSM4()
    c.set_key(key, SM4_DECRYPT)
    if mode.lower() == "cbc":
        if iv is None:
            raise ValueError("SM4-CBC 必须提供 --iv-*")
        return c.crypt_cbc(iv, data)
    return c.crypt_ecb(data)


# --- 国密 SM2（非对称，gmssl）--------------------------------------------------
# key 为 hex 字符串：公钥 128 hex（未压缩点去掉 04 前缀），私钥 64 hex。
# 逆向时常从 JS/APP 里拿到这种 hex 形式。

def _sm2(pub_hex="", priv_hex=""):
    from gmssl import sm2 as _sm2mod
    return _sm2mod.CryptSM2(public_key=pub_hex, private_key=priv_hex)


def sm2_encrypt(data: bytes, pub_hex: str) -> bytes:
    if not pub_hex:
        raise ValueError("SM2 加密需要公钥 --pub-hex（128 hex）")
    return _sm2(pub_hex=pub_hex).encrypt(data)


def sm2_decrypt(data: bytes, priv_hex: str) -> bytes:
    if not priv_hex:
        raise ValueError("SM2 解密需要私钥 --priv-hex（64 hex）")
    return _sm2(priv_hex=priv_hex).decrypt(data)


def _sm2_pub_from_priv(priv_hex: str) -> str:
    """从私钥 hex 推导公钥 hex（128 hex，无 04 前缀）。gmssl 签名需要对象同时带公私钥。"""
    import secrets as _secrets
    from gmssl import sm2 as _sm2mod
    c = _sm2mod.CryptSM2(public_key="", private_key=priv_hex)
    # priv * G，_kg 返回 128 hex（x||y）
    return c._kg(int(priv_hex, 16), c.ecc_table['g'])


def sm2_sign(data: bytes, priv_hex: str) -> str:
    if not priv_hex:
        raise ValueError("SM2 签名需要私钥 --priv-hex（64 hex）")
    import secrets as _secrets
    from gmssl import sm2 as _sm2mod
    pub_hex = _sm2_pub_from_priv(priv_hex)
    c = _sm2mod.CryptSM2(public_key=pub_hex, private_key=priv_hex)
    # gmssl 不传随机 k 时生成的签名无法自洽验签，必须显式传 64 hex 随机 k。
    k = _secrets.token_hex(32)
    return c.sign_with_sm3(data, k)


def sm2_verify(data: bytes, sign_hex: str, pub_hex: str) -> bool:
    if not pub_hex:
        raise ValueError("SM2 验签需要公钥 --pub-hex（128 hex）")
    return _sm2(pub_hex=pub_hex).verify_with_sm3(sign_hex, data)


# --- hash / HMAC（含国密 SM3）--------------------------------------------------

def digest(alg: str, data: bytes) -> str:
    alg = alg.lower()
    if alg == "sm3":
        from gmssl.sm3 import sm3_hash
        from gmssl.func import bytes_to_list
        return sm3_hash(bytes_to_list(data))
    if alg in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512"):
        return hashlib.new(alg, data).hexdigest()
    raise ValueError(f"不支持的 hash 算法: {alg}")


def hmac_digest(alg: str, key: bytes, data: bytes) -> str:
    alg = alg.lower()
    if alg == "sm3":
        # SM3-HMAC：gmssl 无直接封装，按 RFC2104 手工构造（block=64）。
        from gmssl.sm3 import sm3_hash
        from gmssl.func import bytes_to_list
        block = 64
        if len(key) > block:
            key = bytes.fromhex(sm3_hash(bytes_to_list(key)))
        key = key.ljust(block, b"\x00")
        okey = bytes(b ^ 0x5c for b in key)
        ikey = bytes(b ^ 0x36 for b in key)
        inner = bytes.fromhex(sm3_hash(bytes_to_list(ikey + data)))
        return sm3_hash(bytes_to_list(okey + inner))
    if alg in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512"):
        return _hmac.new(key, data, alg).hexdigest()
    raise ValueError(f"不支持的 HMAC 算法: {alg}")


# --- 编码 / JWT ----------------------------------------------------------------

def encode_op(op: str, s: str) -> str:
    op = op.lower()
    if op == "b64e":
        return base64.b64encode(s.encode()).decode()
    if op == "b64d":
        return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
    if op == "urle":
        from urllib.parse import quote
        return quote(s)
    if op == "urld":
        from urllib.parse import unquote
        return unquote(s)
    if op == "hexe":
        return s.encode().hex()
    if op == "hexd":
        return bytes.fromhex(s).decode("utf-8", "replace")
    raise ValueError(f"不支持的编码操作: {op}")


def jwt_decode(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("不是合法 JWT（缺少 . 分隔的三段）")

    def _seg(x):
        return json.loads(base64.urlsafe_b64decode(x + "=" * (-len(x) % 4)))

    return {"header": _seg(parts[0]), "payload": _seg(parts[1]),
            "signature_b64url": parts[2] if len(parts) > 2 else ""}


# --- CLI ----------------------------------------------------------------------

def _emit(result_bytes=None, text=None, as_b64=True):
    if text is not None:
        print(text)
        return
    print(base64.b64encode(result_bytes).decode() if as_b64 else _decode_out(result_bytes))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="加解密/签名/编码多合一工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("aes", "des", "des3", "sm4"):
        sp = sub.add_parser(name)
        sp.add_argument("--op", required=True, choices=["enc", "dec"])
        sp.add_argument("--mode", default="cbc")
        sp.add_argument("--key-hex"); sp.add_argument("--key-b64"); sp.add_argument("--key-utf8")
        sp.add_argument("--iv-hex"); sp.add_argument("--iv-b64"); sp.add_argument("--iv-utf8")
        sp.add_argument("--in-hex"); sp.add_argument("--in-b64"); sp.add_argument("--in-utf8")
        sp.add_argument("--out", choices=["b64", "auto"], default=None)

    hp = sub.add_parser("hash")
    hp.add_argument("--alg", required=True)
    hp.add_argument("--in-hex"); hp.add_argument("--in-b64"); hp.add_argument("--in-utf8")

    mp = sub.add_parser("hmac")
    mp.add_argument("--alg", required=True)
    mp.add_argument("--key-hex"); mp.add_argument("--key-b64"); mp.add_argument("--key-utf8")
    mp.add_argument("--in-hex"); mp.add_argument("--in-b64"); mp.add_argument("--in-utf8")

    ep = sub.add_parser("enc")
    ep.add_argument("--op", required=True,
                    choices=["b64e", "b64d", "urle", "urld", "hexe", "hexd"])
    ep.add_argument("--in", dest="inp", required=True)

    jp = sub.add_parser("jwt")
    jp.add_argument("--token", required=True)

    s2 = sub.add_parser("sm2")
    s2.add_argument("--op", required=True, choices=["enc", "dec", "sign", "verify"])
    s2.add_argument("--pub-hex"); s2.add_argument("--priv-hex")
    s2.add_argument("--sign-hex")  # verify 时的签名值
    s2.add_argument("--in-hex"); s2.add_argument("--in-b64"); s2.add_argument("--in-utf8")
    s2.add_argument("--out", choices=["b64", "auto"], default=None)

    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd in ("aes", "des", "des3", "sm4"):
        key = material(hexv=args.key_hex, b64=args.key_b64, utf8=args.key_utf8)
        iv = None
        if any((args.iv_hex, args.iv_b64, args.iv_utf8)):
            iv = material(hexv=args.iv_hex, b64=args.iv_b64, utf8=args.iv_utf8)
        data = material(hexv=args.in_hex, b64=args.in_b64, utf8=args.in_utf8)
        if cmd == "sm4":
            fn = sm4_encrypt if args.op == "enc" else sm4_decrypt
            out = fn(key, data, mode=args.mode, iv=iv)
        else:
            algo = {"aes": "AES", "des": "DES", "des3": "DES3"}[cmd]
            out = _blockcipher(algo, key, args.mode, iv, data, args.op)
        as_b64 = (args.out or ("b64" if args.op == "enc" else "auto")) == "b64"
        _emit(result_bytes=out, as_b64=as_b64)
    elif cmd == "hash":
        data = material(hexv=args.in_hex, b64=args.in_b64, utf8=args.in_utf8)
        _emit(text=digest(args.alg, data))
    elif cmd == "hmac":
        key = material(hexv=args.key_hex, b64=args.key_b64, utf8=args.key_utf8)
        data = material(hexv=args.in_hex, b64=args.in_b64, utf8=args.in_utf8)
        _emit(text=hmac_digest(args.alg, key, data))
    elif cmd == "enc":
        _emit(text=encode_op(args.op, args.inp))
    elif cmd == "jwt":
        _emit(text=json.dumps(jwt_decode(args.token), ensure_ascii=False, indent=2))
    elif cmd == "sm2":
        data = material(hexv=args.in_hex, b64=args.in_b64, utf8=args.in_utf8)
        if args.op == "enc":
            _emit(result_bytes=sm2_encrypt(data, args.pub_hex),
                  as_b64=(args.out or "b64") == "b64")
        elif args.op == "dec":
            _emit(result_bytes=sm2_decrypt(data, args.priv_hex),
                  as_b64=(args.out or "auto") == "b64")
        elif args.op == "sign":
            _emit(text=sm2_sign(data, args.priv_hex))
        elif args.op == "verify":
            ok = sm2_verify(data, args.sign_hex, args.pub_hex)
            _emit(text="VERIFY_OK" if ok else "VERIFY_FAIL")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 — CLI 里给出干净报错，别抛栈
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
