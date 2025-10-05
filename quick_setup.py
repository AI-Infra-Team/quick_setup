#!/usr/bin/env python3
"""
quick_setup.py — YAML-driven environment bootstrapper (apt + pip)

Usage:
  python3 scripts/quick_setup/quick_setup.py --config /path/to/env.yaml [--dry-run]

YAML schema (minimal):

version: 1
apt:                      # list or mapping
  update: true            # optional, default true
  assume_yes: true        # optional, default true
  options: []             # optional extra apt-get install flags
  packages:               # required if mapping form
    - build-essential
    - git
    - python3-pip
pip:                      # list or mapping
  upgrade: true           # optional, default true
  user: false             # optional, default false; set true for --user
  index_url:              # optional, e.g., https://pypi.tuna.tsinghua.edu.cn/simple
  extra_index_urls: []    # optional list of URLs
  packages:               # required if mapping form
    - PyYAML>=6.0
    - requests

Notes:
- Only apt and pip are considered at present.
- Idempotency is delegated to apt/pip; safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


def ensure_pyyaml_installed() -> None:
    try:
        import yaml  # type: ignore
        _ = yaml  # silence unused
    except Exception:
        print("📦 Installing PyYAML...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "PyYAML"])  # noqa: S603,S607


def load_yaml(path: str) -> Dict[str, Any]:
    ensure_pyyaml_installed()
    import yaml  # type: ignore

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("Top-level YAML must be a mapping (dict)")
        return data


def is_root() -> bool:
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def sudo_prefix() -> List[str]:
    if is_root():
        return []
    if shutil.which("sudo"):
        return ["sudo"]
    # No sudo available; attempts to apt-get will likely fail without root
    return []


def run_cmd(cmd: List[str], env: Optional[Dict[str, str]] = None, dry_run: bool = False) -> int:
    printable = " ".join(cmd)
    print(f"$ {printable}")
    if dry_run:
        return 0
    try:
        return subprocess.call(cmd, env=env)  # noqa: S603
    except FileNotFoundError:
        return 127


def handle_apt(apt_cfg: Any, dry_run: bool) -> None:
    if shutil.which("apt-get") is None:
        print("⚠️  apt-get not found; skipping apt packages.")
        return

    # Normalize config
    update = True
    assume_yes = True
    options: List[str] = []
    packages: List[str] = []

    if isinstance(apt_cfg, list):
        packages = [str(x) for x in apt_cfg]
    elif isinstance(apt_cfg, dict):
        packages = [str(x) for x in apt_cfg.get("packages", [])]
        update = bool(apt_cfg.get("update", True))
        assume_yes = bool(apt_cfg.get("assume_yes", True))
        if isinstance(apt_cfg.get("options"), list):
            options = [str(x) for x in apt_cfg.get("options", [])]
    elif apt_cfg is None:
        packages = []
    else:
        raise ValueError("apt must be a list or mapping")

    if not packages:
        print("✅ No apt packages to install.")
        return

    de_env = os.environ.copy()
    de_env["DEBIAN_FRONTEND"] = "noninteractive"

    if update:
        rc = run_cmd([*sudo_prefix(), "apt-get", "update"], env=de_env, dry_run=dry_run)
        if rc != 0:
            raise SystemExit(rc)

    install_args = ["apt-get", "install"]
    if assume_yes:
        install_args.append("-y")
    install_args.extend(["--no-install-recommends"])  # reduce footprint
    install_args.extend(options)
    install_args.extend(packages)

    rc = run_cmd([*sudo_prefix(), *install_args], env=de_env, dry_run=dry_run)
    if rc != 0:
        raise SystemExit(rc)
    print("✅ apt packages installed.")


def handle_pip(pip_cfg: Any, dry_run: bool) -> None:
    # Normalize config
    packages: List[str] = []
    upgrade = True
    user_install = False
    index_url: Optional[str] = None
    extra_index_urls: List[str] = []
    requirements: List[str] = []

    if isinstance(pip_cfg, list):
        packages = [str(x) for x in pip_cfg]
    elif isinstance(pip_cfg, dict):
        packages = [str(x) for x in pip_cfg.get("packages", [])]
        upgrade = bool(pip_cfg.get("upgrade", True))
        user_install = bool(pip_cfg.get("user", False))
        index_url = pip_cfg.get("index_url")
        if isinstance(pip_cfg.get("extra_index_urls"), list):
            extra_index_urls = [str(x) for x in pip_cfg.get("extra_index_urls", [])]
        # requirements can be a string or list of strings
        reqs = pip_cfg.get("requirements")
        if isinstance(reqs, str):
            requirements = [reqs]
        elif isinstance(reqs, list):
            requirements = [str(x) for x in reqs]
    elif pip_cfg is None:
        packages = []
    else:
        raise ValueError("pip must be a list or mapping")

    if not packages and not requirements:
        print("✅ No pip packages to install.")
        return

    # Choose interpreter consistently
    py = sys.executable or "python3"
    cmd = [py, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    if user_install and not is_root():
        cmd.append("--user")
    if index_url:
        cmd.extend(["--index-url", index_url])
    for url in extra_index_urls:
        cmd.extend(["--extra-index-url", url])
    cmd.extend(packages)
    for req in requirements:
        cmd.extend(["-r", req])

    rc = run_cmd(cmd, dry_run=dry_run)
    if rc != 0:
        raise SystemExit(rc)
    print("✅ pip packages installed.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="YAML-standardized environment setup (apt + pip)")
    parser.add_argument("--config", required=True, help="Path to env YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    args = parser.parse_args(argv)

    cfg = load_yaml(args.config)

    # Optional version field; not enforced yet
    if "version" in cfg:
        try:
            int(cfg["version"])  # validate numeric-like
        except Exception:
            print("⚠️  'version' should be an integer.")

    if "apt" in cfg:
        print("\n==> Installing apt packages")
        handle_apt(cfg.get("apt"), dry_run=args.dry_run)
    else:
        print("\n==> No 'apt' section present; skipping apt.")

    if "pip" in cfg:
        print("\n==> Installing pip packages")
        handle_pip(cfg.get("pip"), dry_run=args.dry_run)
    else:
        print("\n==> No 'pip' section present; skipping pip.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

