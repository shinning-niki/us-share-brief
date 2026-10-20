#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键部署到 GitHub（通过 REST API，无需 git push）

用法：
  GH_TOKEN=github_pat_xxx BARK_URL=https://api.day.app/xxx python scripts/deploy_github.py

做的事：
  1. 校验 token 与账号
  2. 创建仓库（已存在则复用）
  3. 用 Contents API 逐个提交文件
  4. 开启 GitHub Pages（Source = GitHub Actions）
  5. 写入 Actions Secret（BARK_URL）
  6. 触发一次工作流验证

注意（两次踩坑经验）：
  * 写入 .github/workflows/* 需要 token 带 workflow 权限；只有 repo 权限时
    GitHub 会返回 404（伪装成「文件不存在」），极易误判为环境问题。
  * 空仓库上直接用 Git Data API 会返回 409，必须先用 Contents API 建首个提交。

依赖：pynacl（仅用于加密 Secret）
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

REPO_NAME = os.environ.get("REPO_NAME", "us-share-brief")
TOKEN = os.environ.get("GH_TOKEN", "").strip()
BARK = os.environ.get("BARK_URL", "").strip()
WORKFLOW = "daily-brief.yml"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"

SKIP_DIRS = {".git", "__pycache__", "dist"}
SKIP_FILES = {".DS_Store"}


def call(method, path, payload=None, ok=(200, 201, 204)):
    url = path if path.startswith("http") else API + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "us-share-brief-deploy",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body[:300]}
        if e.code in ok:
            return e.code, parsed
        print(f"[ERR] {method} {path} -> {e.code} {parsed.get('message','')}")
        return e.code, parsed


def collect_files():
    out = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if fn in SKIP_FILES or fn.endswith(".pyc"):
                continue
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            with open(full, "rb") as f:
                out[rel] = f.read()
    return out


def main():
    if not TOKEN:
        print("[ERR] 缺少 GH_TOKEN")
        return 1

    code, me = call("GET", "/user")
    if code != 200:
        print("[ERR] token 无效或已过期")
        return 1
    owner = me["login"]
    print(f"[OK] 账号 {owner}")

    code, repo = call("POST", "/user/repos", {
        "name": REPO_NAME,
        "description": "美股收盘简报 · 美东交易日北京时间07:40云端自动生成并推送到手机",
        "private": False,
        "has_issues": False,
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    })
    if code == 201:
        print(f"[OK] 仓库已创建 {owner}/{REPO_NAME}")
    elif code == 422:
        print(f"[INFO] 仓库已存在，复用 {owner}/{REPO_NAME}")
    else:
        return 1
    time.sleep(1)

    c, ref = call("GET", f"/repos/{owner}/{REPO_NAME}/git/ref/heads/main", ok=(200, 404))
    if c != 200:
        print("[INFO] 空仓库，先用 Contents API 建立初始提交")
        seed = base64.b64encode(b"# us-share-brief\n").decode()
        code_seed, _ = call("PUT", f"/repos/{owner}/{REPO_NAME}/contents/.gitkeep", {
            "message": "chore: init", "content": seed, "branch": "main",
        }, ok=(201, 422))
        print(f"[OK] 初始提交就绪（{code_seed}）")
        time.sleep(2)

    files = collect_files()
    print(f"[INFO] 待提交 {len(files)} 个文件")
    ok_count, failed = 0, []
    for path in sorted(files):
        content = files[path]
        payload = {
            "message": f"add: {path}",
            "content": base64.b64encode(content).decode(),
            "branch": "main",
        }
        cg, meta = call("GET", f"/repos/{owner}/{REPO_NAME}/contents/{quote(path)}?ref=main",
                        ok=(200, 404))
        if cg == 200 and isinstance(meta, dict) and meta.get("sha"):
            payload["sha"] = meta["sha"]
        cp, resp = call("PUT", f"/repos/{owner}/{REPO_NAME}/contents/{quote(path)}", payload,
                        ok=(200, 201))
        if cp in (200, 201):
            ok_count += 1
            print(f"  [OK] {path}")
        else:
            failed.append(path)
            print(f"  [ERR] {path} -> {cp} {str(resp.get('message',''))[:80]}")
    print(f"[OK] 已上传 {ok_count}/{len(files)} 个文件")
    if failed:
        print("[WARN] 失败清单：" + ", ".join(failed))
        if any(p.startswith(".github/workflows") for p in failed):
            print("[HINT] 工作流文件写入失败通常是 token 缺 workflow 权限（会伪装成 404）")
    if ok_count == 0:
        return 1

    code, _ = call("POST", f"/repos/{owner}/{REPO_NAME}/pages", {"build_type": "workflow"})
    if code in (201, 204):
        print("[OK] Pages 已开启（Source: GitHub Actions）")
    elif code == 409:
        print("[INFO] Pages 已存在")
    else:
        print("[WARN] Pages 开启失败，请到 Settings → Pages 手动选 GitHub Actions")

    if BARK:
        try:
            sys.path.insert(0, "/tmp/pylibs")
            from nacl import encoding, public
        except ImportError:
            print("[WARN] 缺少 pynacl，Secret 未写入，请手动添加 BARK_URL")
        else:
            c, pk = call("GET", f"/repos/{owner}/{REPO_NAME}/actions/secrets/public-key")
            if c == 200:
                pk_obj = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
                sealed = public.SealedBox(pk_obj).encrypt(BARK.encode())
                enc = base64.b64encode(sealed).decode()
                c2, _ = call("PUT", f"/repos/{owner}/{REPO_NAME}/actions/secrets/BARK_URL",
                             {"encrypted_value": enc, "key_id": pk["key_id"]}, ok=(201, 204))
                print("[OK] Secret BARK_URL 已写入" if c2 in (201, 204)
                      else "[WARN] Secret 写入失败")

    time.sleep(2)
    c, _ = call("POST", f"/repos/{owner}/{REPO_NAME}/actions/workflows/{WORKFLOW}/dispatches",
                {"ref": "main"}, ok=(204,))
    print("[OK] 已触发工作流" if c == 204 else "[WARN] 触发失败，可在 Actions 页面手动 Run")

    print(f"\n仓库：https://github.com/{owner}/{REPO_NAME}")
    print(f"页面：https://{owner}.github.io/{REPO_NAME}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
