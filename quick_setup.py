#!/usr/bin/env python3
"""
quick_setup.py — YAML-driven environment bootstrapper (apt + pip + scripts)

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

scripts:                  # optional, list of strings or mappings
  - echo "hello"          # string -> executed via bash -lc in config dir
  - cmd: ["python3", "tools/setup.py", "--flag"]  # list -> executed directly
    cwd: tools            # optional working dir (relative to config)
    env: { FOO: BAR }     # optional env overrides

Notes:
- Supports apt, pip, and custom scripts.
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


def handle_pip(pip_cfg: Any, dry_run: bool, base_dir: Optional[str] = None) -> None:
    # Normalize config
    packages: List[Any] = []
    upgrade = True
    user_install = False
    index_url: Optional[str] = None
    extra_index_urls: List[str] = []
    requirements: List[str] = []

    if isinstance(pip_cfg, list):
        packages = [x for x in pip_cfg]
    elif isinstance(pip_cfg, dict):
        packages = [x for x in pip_cfg.get("packages", [])]
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
    # Resolve special package mappings (e.g., { pyproject: "./" }) relative to YAML dir
    resolved_packages: List[str] = []
    for item in packages:
        if isinstance(item, dict) and "pyproject" in item:
            rel = item.get("pyproject")
            if rel is None:
                raise ValueError("pip packages item 'pyproject' requires a path")
            rel_str = str(rel)
            # Determine absolute path relative to base_dir (YAML location)
            abs_path = os.path.abspath(os.path.join(base_dir or os.getcwd(), rel_str)) if not os.path.isabs(rel_str) else rel_str
            # Accept directory, or setup.py / pyproject.toml file and convert to its directory
            if os.path.isdir(abs_path):
                project_path = abs_path
            elif os.path.isfile(abs_path) and os.path.basename(abs_path) in ("setup.py", "pyproject.toml"):
                project_path = os.path.dirname(abs_path)
            else:
                raise FileNotFoundError(f"pyproject path not found or invalid: {abs_path}")
            # Optional: allow forcing editable install, only if explicitly requested
            force_editable = bool(item.get("editable", False))
            if force_editable:
                resolved_packages.extend(["-e", project_path])
            else:
                # If an old root-owned build/ exists and isn't writable, try to clean it up
                build_dir = os.path.join(project_path, "build")
                try:
                    if os.path.isdir(build_dir) and not os.access(build_dir, os.W_OK):
                        print(f"⚠️  Detected non-writable build dir: {build_dir} — attempting cleanup via sudo")
                        rc = run_cmd([*sudo_prefix(), "rm", "-rf", build_dir], dry_run=dry_run)
                        if rc != 0:
                            print("⚠️  Cleanup failed; pip install may fail if build dir remains read-only.")
                    # Also clean non-writable egg-info dirs at project root
                    for name in os.listdir(project_path):
                        if name.endswith('.egg-info'):
                            egg_dir = os.path.join(project_path, name)
                            if os.path.isdir(egg_dir) and not os.access(egg_dir, os.W_OK):
                                print(f"⚠️  Detected non-writable egg-info: {egg_dir} — attempting cleanup via sudo")
                                rc = run_cmd([*sudo_prefix(), "rm", "-rf", egg_dir], dry_run=dry_run)
                                if rc != 0:
                                    print("⚠️  Cleanup failed; pip install may fail if egg-info remains read-only.")
                except Exception:
                    # Non-critical; proceed and let pip report if it fails
                    pass
                resolved_packages.append(project_path)
        else:
            resolved_packages.append(str(item))

    cmd.extend(resolved_packages)
    for req in requirements:
        cmd.extend(["-r", req])

    rc = run_cmd(cmd, dry_run=dry_run)
    if rc != 0:
        raise SystemExit(rc)
    print("✅ pip packages installed.")


def _as_cwd(path: Optional[str], base_dir: str) -> str:
    if not path:
        return base_dir
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(base_dir, path))


def handle_scripts(scripts_cfg: Any, dry_run: bool, base_dir: str) -> None:
    """Execute custom script commands.

    Supported forms:
    - list of strings: each executed via bash -lc in base_dir
    - list of mappings: { cmd: <str|[args...]>, cwd?: <str>, env?: {K:V} }
    """
    if not scripts_cfg:
        return

    items: List[Any]
    if isinstance(scripts_cfg, list):
        items = scripts_cfg
    else:
        raise ValueError("scripts must be a list of strings or mappings")

    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            cmd = ["bash", "-lc", item]
            cwd = base_dir
            env = os.environ.copy()
        elif isinstance(item, dict):
            cmd_val = item.get("cmd")
            if isinstance(cmd_val, list):
                cmd = [str(x) for x in cmd_val]
            elif isinstance(cmd_val, str):
                cmd = ["bash", "-lc", cmd_val]
            else:
                raise ValueError("scripts item mapping requires 'cmd' as str or list")
            cwd = _as_cwd(item.get("cwd"), base_dir)
            env = os.environ.copy()
            env_cfg = item.get("env") or {}
            if isinstance(env_cfg, dict):
                env.update({str(k): str(v) for k, v in env_cfg.items()})
        else:
            raise ValueError("scripts items must be strings or mappings")

        print(f"-- script[{i}] cwd={cwd}")
        printable = " ".join(cmd)
        print(f"$ {printable}")
        if not dry_run:
            rc = subprocess.call(cmd, cwd=cwd, env=env)  # noqa: S603
            if rc != 0:
                raise SystemExit(rc)


def _extract_groups(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return a mapping of group_name -> group_cfg.

    Supports:
    - New style: { groups: { base: {...}, rust: {...}, mooncake: {...} } }
    - Flat named groups at top level: { base: {...}, rust: {...} }
    - Legacy flat config: { apt: ..., pip: ... } as single group 'default'
    """
    if isinstance(cfg.get("groups"), dict):
        groups = cfg["groups"]
        return {str(k): (v or {}) for k, v in groups.items()}

    known = [k for k in ("base", "rust", "mooncake") if isinstance(cfg.get(k), dict)]
    if known:
        return {k: cfg[k] for k in known}

    return {"default": cfg}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="YAML-standardized environment setup (apt + pip) — installs all groups")
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

    groups = _extract_groups(cfg)

    # Always install all groups in YAML order
    for name, gcfg in groups.items():

        print(f"\n==> Group [{name}]")
        if "apt" in gcfg:
            print("-- apt")
            handle_apt(gcfg.get("apt"), dry_run=args.dry_run)
        if "pip" in gcfg:
            print("-- pip")
            config_dir = os.path.dirname(os.path.abspath(args.config))
            handle_pip(gcfg.get("pip"), dry_run=args.dry_run, base_dir=config_dir)

        if "scripts" in gcfg:
            print("-- scripts")
            config_dir = os.path.dirname(os.path.abspath(args.config))
            handle_scripts(gcfg.get("scripts"), dry_run=args.dry_run, base_dir=config_dir)

        # Legacy single group fields
        if "apt" not in gcfg and name == "default" and "apt" in cfg:
            print("-- apt")
            handle_apt(cfg.get("apt"), dry_run=args.dry_run)
        if "pip" not in gcfg and name == "default" and "pip" in cfg:
            print("-- pip")
            config_dir = os.path.dirname(os.path.abspath(args.config))
            handle_pip(cfg.get("pip"), dry_run=args.dry_run, base_dir=config_dir)

        if "scripts" not in gcfg and name == "default" and "scripts" in cfg:
            print("-- scripts")
            config_dir = os.path.dirname(os.path.abspath(args.config))
            handle_scripts(cfg.get("scripts"), dry_run=args.dry_run, base_dir=config_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
