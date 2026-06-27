#!/usr/bin/env python3
r"""
Windows llama-cpp-python doctor and installer for ComfyUI.

Default mode is read-only:
    python tools/llama_cpp_windows_doctor.py --comfy-root C:\ComfyUI_windows_portable\ComfyUI

Install mode requires an explicit flag:
    python tools/llama_cpp_windows_doctor.py --comfy-root C:\ComfyUI_windows_portable\ComfyUI --install
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RELEASES_API = "https://api.github.com/repos/JamePeng/llama-cpp-python/releases?per_page=30"
WHEEL_RE = re.compile(
    r"^llama_cpp_python-(?P<version>[^+]+)\+(?P<cuda>cu\d+)-"
    r"(?P<abi>cp\d+)-(?P=abi)-(?P<platform>win_amd64)\.whl$"
)


@dataclass
class WheelAsset:
    name: str
    url: str
    release: str
    published_at: str
    version: str
    cuda: str
    abi: str


def run_command(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def decode_json_process(proc: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    if proc.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON:\n{proc.stdout}") from exc


def candidate_python_paths(comfy_root: Path | None, explicit_python: Path | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_python:
        candidates.append(explicit_python)

    roots: list[Path] = []
    if comfy_root:
        roots.append(comfy_root)
        roots.append(comfy_root.parent)

    # If the script is inside ComfyUI/custom_nodes/LLM_Prompt/tools, infer ComfyUI.
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if parent.name.lower() == "custom_nodes":
            roots.append(parent.parent)
            roots.append(parent.parent.parent)
            break

    for root in roots:
        candidates.extend(
            [
                root / "python_embeded" / "python.exe",
                root / "python_embedded" / "python.exe",
                root / "venv" / "Scripts" / "python.exe",
                root / ".venv" / "Scripts" / "python.exe",
            ]
        )

    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def find_python(comfy_root: Path | None, explicit_python: Path | None) -> Path:
    for candidate in candidate_python_paths(comfy_root, explicit_python):
        if candidate.is_file():
            return candidate
    tried = "\n".join(f"  - {path}" for path in candidate_python_paths(comfy_root, explicit_python))
    raise SystemExit(
        "Could not find ComfyUI Python. Pass --python with the exact python.exe path.\n"
        f"Tried:\n{tried}"
    )


def inspect_python(py: Path) -> dict[str, Any]:
    code = r"""
import glob
import importlib.metadata
import json
import os
import platform
import site
import sys

data = {
    "executable": sys.executable,
    "version": sys.version,
    "major": sys.version_info.major,
    "minor": sys.version_info.minor,
    "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
    "platform": platform.platform(),
    "machine": platform.machine(),
    "site_packages": site.getsitepackages(),
    "torch": None,
    "llama_cpp": None,
}

try:
    import torch
    data["torch"] = {
        "version": getattr(torch, "__version__", None),
        "cuda": getattr(getattr(torch, "version", None), "cuda", None),
        "cuda_available": bool(torch.cuda.is_available()),
        "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
        "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }
except Exception as exc:
    data["torch"] = {"error": repr(exc)}

try:
    import llama_cpp
    import llama_cpp.llama_cpp as lc

    dist_version = None
    try:
        dist_version = importlib.metadata.version("llama-cpp-python")
    except Exception:
        pass

    lib_name = getattr(getattr(lc, "_lib", None), "_name", None)
    supports_gpu_offload = None
    try:
        if hasattr(lc, "llama_backend_init"):
            lc.llama_backend_init()
        if hasattr(lc, "llama_supports_gpu_offload"):
            supports_gpu_offload = bool(lc.llama_supports_gpu_offload())
    except Exception as exc:
        supports_gpu_offload = f"error: {exc!r}"

    direct_urls = []
    dist_infos = []
    cuda_dlls = []
    for base in site.getsitepackages():
        dist_infos.extend(glob.glob(os.path.join(base, "llama_cpp_python*.dist-info")))
        cuda_dlls.extend(glob.glob(os.path.join(base, "llama_cpp", "lib", "*cuda*")))
        cuda_dlls.extend(glob.glob(os.path.join(base, "llama_cpp", "bin", "*cuda*")))

    for dist in dist_infos:
        direct = os.path.join(dist, "direct_url.json")
        if os.path.exists(direct):
            try:
                with open(direct, "r", encoding="utf-8") as handle:
                    direct_urls.append(json.load(handle))
            except Exception as exc:
                direct_urls.append({"error": repr(exc), "path": direct})

    data["llama_cpp"] = {
        "version": getattr(llama_cpp, "__version__", None) or dist_version,
        "dist_version": dist_version,
        "lib": lib_name,
        "supports_gpu_offload": supports_gpu_offload,
        "dist_infos": dist_infos,
        "direct_urls": direct_urls,
        "cuda_dlls": cuda_dlls,
    }
except Exception as exc:
    data["llama_cpp"] = {"error": repr(exc)}

print(json.dumps(data, indent=2))
"""
    proc = run_command([str(py), "-c", code])
    return decode_json_process(proc, "Python inspection")


def inspect_nvidia_smi() -> dict[str, Any]:
    proc = run_command(["nvidia-smi"])
    return {
        "available": proc.returncode == 0,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def torch_cuda_to_selector(cuda_version: str | None) -> str | None:
    if not cuda_version:
        return None
    match = re.match(r"^(\d+)\.(\d+)", cuda_version.strip())
    if not match:
        return None
    major, minor = match.groups()
    return f"cu{major}{minor}"


def fetch_jamepeng_releases() -> list[dict[str, Any]]:
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "LLM-Prompt-llama-cpp-doctor",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not query JamePeng GitHub releases: {exc}") from exc


def collect_assets(releases: list[dict[str, Any]]) -> list[WheelAsset]:
    assets: list[WheelAsset] = []
    for release in releases:
        for asset in release.get("assets", []):
            name = asset.get("name") or ""
            match = WHEEL_RE.match(name)
            if not match:
                continue
            assets.append(
                WheelAsset(
                    name=name,
                    url=asset.get("browser_download_url") or "",
                    release=release.get("tag_name") or "",
                    published_at=release.get("published_at") or "",
                    version=match.group("version"),
                    cuda=match.group("cuda"),
                    abi=match.group("abi"),
                )
            )
    return assets


def select_wheel(assets: list[WheelAsset], *, abi: str, cuda: str, version: str | None) -> WheelAsset | None:
    matches = [asset for asset in assets if asset.abi == abi and asset.cuda == cuda]
    if version:
        matches = [asset for asset in matches if asset.version == version]
    return matches[0] if matches else None


def format_value(value: Any) -> str:
    if value is None:
        return "not detected"
    if value == "":
        return "empty"
    return str(value)


def print_report(report: dict[str, Any]) -> None:
    py = report["python"]
    torch = py.get("torch") or {}
    llama = py.get("llama_cpp") or {}
    selected = report.get("selected_wheel")

    print_section("ComfyUI Python")
    print(f"Python: {py.get('version', '').splitlines()[0]}")
    print(f"Executable: {py.get('executable')}")
    print(f"ABI: {py.get('abi')}")
    print(f"Platform: {py.get('platform')}")

    print_section("Torch / CUDA")
    if "error" in torch:
        print(f"Torch: ERROR {torch['error']}")
    else:
        print(f"Torch: {format_value(torch.get('version'))}")
        print(f"Torch CUDA: {format_value(torch.get('cuda'))}")
        print(f"CUDA selector: {format_value(report.get('cuda_selector'))}")
        print(f"CUDA available: {format_value(torch.get('cuda_available'))}")
        print(f"GPU: {format_value(torch.get('device_name'))}")

    print_section("Existing llama-cpp-python")
    if "error" in llama:
        print(f"Installed: no or broken import ({llama['error']})")
    else:
        print(f"Version: {format_value(llama.get('version'))}")
        print(f"Library: {format_value(llama.get('lib'))}")
        print(f"CUDA DLLs found: {len(llama.get('cuda_dlls') or [])}")
        print(f"GPU offload support flag: {format_value(llama.get('supports_gpu_offload'))}")
        direct_urls = llama.get("direct_urls") or []
        if direct_urls:
            first_url = direct_urls[0].get("url") if isinstance(direct_urls[0], dict) else None
            print(f"Installed from: {format_value(first_url)}")

    print_section("Recommended Wheel")
    if selected:
        print(f"Release: {selected['release']}")
        print(f"Wheel: {selected['name']}")
        print(f"URL: {selected['url']}")
    elif report.get("manual_wheel"):
        print(f"Manual wheel override: {report['manual_wheel']}")
    else:
        print("No exact JamePeng wheel match found.")
        print("Check Python ABI and Torch CUDA, or use --wheel with a known wheel URL/path.")

    print_section("Next Action")
    for line in report.get("advice", []):
        print(f"- {line}")


def install_wheel(py: Path, wheel: str, *, yes: bool) -> None:
    if not yes:
        print("\nAbout to change the ComfyUI Python environment.")
        print(f"Python: {py}")
        print(f"Wheel: {wheel}")
        answer = input("Continue? Type YES to install: ").strip()
        if answer != "YES":
            raise SystemExit("Install cancelled.")

    print_section("Uninstall existing llama-cpp-python")
    uninstall = subprocess.run([str(py), "-m", "pip", "uninstall", "-y", "llama-cpp-python"])
    if uninstall.returncode != 0:
        raise SystemExit(f"pip uninstall failed with exit code {uninstall.returncode}")

    print_section("Install selected wheel")
    install = subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--force-reinstall",
            "--no-deps",
            wheel,
        ]
    )
    if install.returncode != 0:
        raise SystemExit(f"pip install failed with exit code {install.returncode}")


def run_smoke_test(py: Path, model: Path) -> dict[str, Any]:
    code = (
        "from llama_cpp import Llama; import json, sys; "
        "llm=Llama(model_path=sys.argv[1], n_gpu_layers=-1, n_ctx=512, verbose=True); "
        "out=llm('Say OK', max_tokens=8); "
        "print(json.dumps({'text': out['choices'][0]['text']}))"
    )
    proc = run_command([str(py), "-c", code, str(model)])
    return {
        "model": str(model),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def build_advice(report: dict[str, Any], install_requested: bool) -> list[str]:
    advice: list[str] = []
    py = report["python"]
    torch = py.get("torch") or {}
    llama = py.get("llama_cpp") or {}

    if platform.system().lower() != "windows":
        advice.append("This helper is designed for Windows wheels.")

    if "error" in torch:
        advice.append("Torch could not be imported from this Python. Check that this is the ComfyUI Python.")
    elif not torch.get("cuda"):
        advice.append("Torch CUDA was not detected. A CUDA JamePeng wheel cannot be selected automatically.")
    elif not torch.get("cuda_available"):
        advice.append("Torch reports CUDA is not available. Check the NVIDIA driver and the active ComfyUI environment.")

    if not report.get("selected_wheel") and not report.get("manual_wheel"):
        advice.append("No install will be attempted because no exact wheel was selected.")
    elif install_requested:
        advice.append("Install requested. Close ComfyUI before continuing.")
    else:
        advice.append("Dry run only. Add --install to install the selected wheel.")

    if "error" in llama:
        advice.append("Existing llama-cpp-python is missing or broken.")
    elif len(llama.get("cuda_dlls") or []) == 0:
        advice.append("Existing llama-cpp-python has no CUDA DLLs visible.")

    return advice


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect ComfyUI Python and install the matching JamePeng llama-cpp-python wheel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python tools\\llama_cpp_windows_doctor.py --comfy-root C:\\ComfyUI_windows_portable\\ComfyUI
              python tools\\llama_cpp_windows_doctor.py --python E:\\ComfyUI-Easy-Install\\python_embeded\\python.exe
              python tools\\llama_cpp_windows_doctor.py --python E:\\ComfyUI-Easy-Install\\python_embeded\\python.exe --install
            """
        ),
    )
    parser.add_argument("--comfy-root", type=Path, help="Path to the ComfyUI folder or portable install root.")
    parser.add_argument("--python", type=Path, help="Exact ComfyUI python.exe path.")
    parser.add_argument("--wheel", help="Manual wheel URL or local .whl path. Skips GitHub wheel selection.")
    parser.add_argument("--version", help="Require a specific llama-cpp-python version, for example 0.3.40.")
    parser.add_argument("--install", action="store_true", help="Install the selected wheel. Default is read-only.")
    parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation when --install is used.")
    parser.add_argument("--list-matches", action="store_true", help="Print all matching JamePeng wheels for this system.")
    parser.add_argument("--smoke-model", type=Path, help="Optional GGUF model path for a post-install smoke test.")
    parser.add_argument("--report", type=Path, help="Write a JSON report to this path.")
    args = parser.parse_args()

    py = find_python(args.comfy_root, args.python)
    inspection = inspect_python(py)
    torch_info = inspection.get("torch") or {}
    cuda_selector = torch_cuda_to_selector(torch_info.get("cuda"))
    selected: WheelAsset | None = None
    assets: list[WheelAsset] = []

    if not args.wheel and cuda_selector:
        releases = fetch_jamepeng_releases()
        assets = collect_assets(releases)
        selected = select_wheel(
            assets,
            abi=inspection["abi"],
            cuda=cuda_selector,
            version=args.version,
        )

    if args.list_matches and assets:
        print_section("Matching Wheels")
        for asset in assets:
            if asset.abi == inspection["abi"] and asset.cuda == cuda_selector:
                print(f"{asset.release}: {asset.name}")
                print(f"  {asset.url}")

    wheel = args.wheel or (selected.url if selected else None)
    report: dict[str, Any] = {
        "python": inspection,
        "nvidia_smi": inspect_nvidia_smi(),
        "cuda_selector": cuda_selector,
        "selected_wheel": selected.__dict__ if selected else None,
        "manual_wheel": args.wheel,
        "install_requested": args.install,
        "install_attempted": False,
        "install_success": None,
        "smoke_test": None,
    }
    report["advice"] = build_advice(report, args.install)

    print_report(report)

    if args.install:
        if not wheel:
            raise SystemExit("No wheel selected. Use --wheel with a URL/path or fix the detected CUDA/Python state.")
        install_wheel(py, wheel, yes=args.yes)
        report["install_attempted"] = True
        report["install_success"] = True

        print_section("Post-install Verification")
        post = inspect_python(py)
        report["post_install_python"] = post
        llama = post.get("llama_cpp") or {}
        if "error" in llama:
            report["install_success"] = False
            print(f"Import failed after install: {llama['error']}")
        else:
            print(f"Version: {format_value(llama.get('version'))}")
            print(f"Library: {format_value(llama.get('lib'))}")
            print(f"CUDA DLLs found: {len(llama.get('cuda_dlls') or [])}")

    if args.smoke_model:
        print_section("GGUF Smoke Test")
        smoke = run_smoke_test(py, args.smoke_model)
        report["smoke_test"] = smoke
        print(f"Return code: {smoke['returncode']}")
        if smoke["stdout"]:
            print(smoke["stdout"])
        if smoke["stderr"]:
            print(smoke["stderr"])

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nReport written: {args.report}")

    return 0 if report.get("install_success") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
