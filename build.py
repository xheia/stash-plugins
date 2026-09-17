#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包插件并刷新插件索引。

产物：
  dist/<pluginID>-v<version>.zip   内容为 <pluginID>/... ，可直接解压进 Stash 的 plugins 目录
  plugins/main/index.yml           插件源索引，Stash 在「设置 -> 插件 -> 添加源」里填它的地址

用法：
    python3 build.py                      # 用默认的 GitHub Release 地址
    python3 build.py --base-url https://example.com/pkgs
    python3 build.py --check              # 只校验不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import zipfile
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ID = "translateMetadata"
SRC_DIR = os.path.join(ROOT, "src", PLUGIN_ID)
MANIFEST = os.path.join(SRC_DIR, PLUGIN_ID + ".yml")
DIST_DIR = os.path.join(ROOT, "dist")
INDEX_PATH = os.path.join(ROOT, "plugins", "main", "index.yml")
DEFAULT_REPO = "xheia/stash-plugins"

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {"translate-cache.sqlite3", ".DS_Store"}
EXCLUDE_EXT = {".pyc", ".pyo", ".sqlite3", ".log"}


def read_version():
    with open(MANIFEST, "r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    return str(manifest.get("version") or "0.0.0"), manifest


def iter_files():
    """遍历插件目录，产出 (绝对路径, zip 内相对路径)。"""
    for current, dirs, files in os.walk(SRC_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in sorted(files):
            if name in EXCLUDE_FILES or os.path.splitext(name)[1] in EXCLUDE_EXT:
                continue
            full = os.path.join(current, name)
            rel = os.path.relpath(full, SRC_DIR).replace("\\", "/")
            yield full, "%s/%s" % (PLUGIN_ID, rel)


def build_zip(version, check_only=False):
    os.makedirs(DIST_DIR, exist_ok=True)
    zip_name = "%s-v%s.zip" % (PLUGIN_ID, version)
    zip_path = os.path.join(DIST_DIR, zip_name)

    if check_only:
        print("将打包以下文件：")
        for full, rel in iter_files():
            print("  %s" % rel)
        return zip_path, None, zip_name

    if os.path.exists(zip_path):
        os.remove(zip_path)

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, rel in iter_files():
            # 固定时间戳 + 固定权限 + 固定 create_system，保证同样内容产出同样的 zip。
            # 尤其是 create_system：zipfile 默认按平台取（Windows=0，Unix=3），
            # 不写死的话，Windows 与 CI 的 Linux runner 会产出内容相同但字节不同的 zip，
            # sha256 对不上，索引里的校验值也就没法横向比对。
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3          # 3 = Unix，跨平台一致
            info.external_attr = 0o644 << 16
            with open(full, "rb") as fh:
                zf.writestr(info, fh.read())
            count += 1
            print("  + %s" % rel)

    digest = sha256_of(zip_path)
    size = os.path.getsize(zip_path)
    print("\n已生成 %s（%d 个文件，%.1f KB）" % (
        os.path.relpath(zip_path, ROOT), count, size / 1024.0))
    print("sha256: %s" % digest)
    return zip_path, digest, zip_name


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_index(version, manifest, digest, base_url, check_only=False):
    entry = {
        "id": PLUGIN_ID,
        "name": manifest.get("name") or PLUGIN_ID,
        "metadata": {
            "description": " ".join(str(manifest.get("description") or "").split()),
        },
        "version": version,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "path": "%s/%s-v%s.zip" % (base_url.rstrip("/"), PLUGIN_ID, version),
        "sha256": digest,
    }

    entries = []
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as handle:
            entries = yaml.safe_load(handle) or []

    entries = [e for e in entries if e.get("id") != PLUGIN_ID]
    entries.append(entry)
    entries.sort(key=lambda e: str(e.get("id")))
    # 把我们的插件放到最前面，方便在 Stash 里一眼看到
    entries.sort(key=lambda e: 0 if e.get("id") == PLUGIN_ID else 1)

    lines = []
    for item in entries:
        lines.append("- id: %s" % item["id"])
        lines.append("  name: %s" % item["name"])
        lines.append("  metadata:")
        lines.append("    description: %s" % item["metadata"]["description"])
        lines.append("  version: %s" % item["version"])
        lines.append("  date: %s" % item["date"])
        lines.append("  path: %s" % item["path"])
        lines.append("  sha256: %s" % item["sha256"])
    text = "\n".join(lines) + "\n"

    if check_only:
        print("\n将写入 %s：\n%s" % (os.path.relpath(INDEX_PATH, ROOT), text))
        return

    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("\n已更新 %s" % os.path.relpath(INDEX_PATH, ROOT))


def main():
    parser = argparse.ArgumentParser(description="打包 translateMetadata 插件")
    parser.add_argument("--base-url", default=None,
                        help="zip 的下载地址前缀，默认指向 GitHub Release")
    parser.add_argument("--check", action="store_true", help="只校验与预览，不写文件")
    parser.add_argument("--clean", action="store_true", help="先清空 dist 目录")
    args = parser.parse_args()

    version, manifest = read_version()
    base_url = args.base_url or (
        "https://github.com/%s/releases/download/v%s" % (DEFAULT_REPO, version))

    print("插件: %s 版本: %s\n" % (manifest.get("name"), version))

    if args.clean and os.path.isdir(DIST_DIR) and not args.check:
        shutil.rmtree(DIST_DIR)
        print("已清空 dist/\n")

    _, digest, _ = build_zip(version, args.check)

    if args.check:
        print("\n（--check 模式：未生成 zip，索引仅预览）")
        # 预览索引时给个占位摘要，避免误解
        update_index(version, manifest, "<sha256>", base_url, check_only=True)
        return 0

    update_index(version, manifest, digest, base_url)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
