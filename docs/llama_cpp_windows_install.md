# Windows llama-cpp-python GGUF Install Guide

This guide is for users who want to run the local `LLM Prompt` node with GGUF models on Windows.

The difficult part is not the node itself. The difficult part is choosing a `llama-cpp-python` wheel that matches the user's ComfyUI Python and CUDA stack. Installing the wrong wheel can make the local GGUF node fail to import, run on CPU, or crash when a model loads.

This node is tested with JamePeng's `llama-cpp-python` builds because those builds include handler support used by modern Gemma and Qwen GGUF models.

## Automatic Doctor / Installer

This repository includes a helper script:

```text
tools/llama_cpp_windows_doctor.py
```

Default mode is read-only. It inspects the selected ComfyUI Python, checks Python ABI, Torch CUDA, the existing `llama-cpp-python` install, queries JamePeng GitHub releases, and recommends the matching wheel.

Run it from the node folder:

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
& $py tools\llama_cpp_windows_doctor.py --comfy-root E:\ComfyUI-Easy-Install\ComfyUI
```

Or pass the exact embedded Python:

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
& $py tools\llama_cpp_windows_doctor.py --python $py
```

To install the recommended wheel, close ComfyUI first, then run:

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
& $py tools\llama_cpp_windows_doctor.py --python $py --install
```

To save a report:

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
& $py tools\llama_cpp_windows_doctor.py --python $py --report llama_cpp_report.json
```

The manual steps below explain what the tool is checking.

## What Must Match

Choose the wheel for the ComfyUI Python environment, not for system Python.

The wheel must match:

| Local value | Example | Wheel part |
| --- | --- | --- |
| Python ABI | Python 3.12 | `cp312-cp312` |
| Operating system | Windows 64-bit | `win_amd64` |
| CUDA build family | CUDA 12.8 / 13.0, etc. | `cu128`, `cu130`, etc. |
| llama-cpp-python version | `0.3.40` | `llama_cpp_python-0.3.40...whl` |

Use the CUDA version reported by PyTorch inside ComfyUI as the main CUDA selector. The CUDA version shown by `nvidia-smi` is the driver runtime capability; it does not always equal the PyTorch wheel CUDA build.

## Step 1: Find ComfyUI Python

Portable ComfyUI builds often use an embedded Python such as:

```powershell
E:\ComfyUI-Easy-Install\python_embeded\python.exe
```

Official portable ComfyUI often uses:

```powershell
ComfyUI_windows_portable\python_embeded\python.exe
```

Set a variable for the rest of the commands:

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
```

Change the path if your ComfyUI install is somewhere else.

## Step 2: Inspect Python

```powershell
& $py -c "import sys, platform; print(sys.version); print(sys.executable); print(platform.platform())"
```

Map Python version to wheel ABI:

| Python version | Wheel ABI |
| --- | --- |
| 3.10.x | `cp310-cp310` |
| 3.11.x | `cp311-cp311` |
| 3.12.x | `cp312-cp312` |
| 3.13.x | `cp313-cp313` |

Example:

```text
Python 3.12.x -> cp312-cp312
```

## Step 3: Inspect Torch and CUDA

```powershell
& $py -c "import torch; print('torch', torch.__version__); print('torch_cuda', torch.version.cuda); print('cuda_available', torch.cuda.is_available()); print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

Examples:

| Torch output | Wheel CUDA selector |
| --- | --- |
| `2.8.x+cu128` / `torch.version.cuda = 12.8` | `cu128` |
| `2.9.x+cu130` / `torch.version.cuda = 13.0` | `cu130` |

Also check the NVIDIA driver:

```powershell
nvidia-smi
```

The driver must be new enough for the CUDA family you are trying to use.

## Step 4: Inspect Existing llama-cpp-python

```powershell
& $py -m pip show llama-cpp-python
```

If it is already installed, inspect the wheel source:

```powershell
$site = (& $py -c "import site; print(site.getsitepackages()[0])")
Get-ChildItem $site -Directory -Filter 'llama_cpp_python*.dist-info'
```

Then read `direct_url.json` if it exists:

```powershell
Get-Content "$site\llama_cpp_python-0.3.40.dist-info\direct_url.json"
```

The folder version may differ from `0.3.40`; use the folder you actually have.

## Step 5: Choose the JamePeng Wheel

Open the JamePeng releases page:

```text
https://github.com/JamePeng/llama-cpp-python/releases
```

Pick a release that matches your CUDA family and Windows.

Wheel filename pattern:

```text
llama_cpp_python-<version>+<cuda>-<python_abi>-<python_abi>-win_amd64.whl
```

Example for:

- Python 3.12
- Torch CUDA 13.0
- Windows 64-bit
- llama-cpp-python 0.3.40

The matching wheel is:

```text
llama_cpp_python-0.3.40+cu130-cp312-cp312-win_amd64.whl
```

Example URL:

```text
https://github.com/JamePeng/llama-cpp-python/releases/download/v0.3.40-cu130-win-20260608/llama_cpp_python-0.3.40+cu130-cp312-cp312-win_amd64.whl
```

Do not install a `cp311` wheel into Python 3.12. Do not install a `cu128` wheel just because another user used it. Match your own ComfyUI Python and Torch CUDA.

## Step 6: Install the Wheel

Close ComfyUI first.

```powershell
$py = 'E:\ComfyUI-Easy-Install\python_embeded\python.exe'
$wheel = 'https://github.com/JamePeng/llama-cpp-python/releases/download/v0.3.40-cu130-win-20260608/llama_cpp_python-0.3.40+cu130-cp312-cp312-win_amd64.whl'

& $py -m pip uninstall -y llama-cpp-python
& $py -m pip install --no-cache-dir --force-reinstall --no-deps $wheel
```

Keep `--no-deps`. ComfyUI already has a carefully matched Python stack. Letting pip replace dependencies can break Torch, NumPy, or other custom nodes.

## Step 7: Verify Import

```powershell
& $py -c "import llama_cpp, llama_cpp.llama_cpp as lc; print('llama_cpp', getattr(llama_cpp, '__version__', 'unknown')); print('lib', getattr(lc._lib, '_name', 'unknown'))"
```

A healthy install prints the version and a `llama.dll` path from the ComfyUI Python `site-packages`.

## Step 8: Verify CUDA Files Are Present

```powershell
$site = (& $py -c "import site; print(site.getsitepackages()[0])")
Get-ChildItem "$site\llama_cpp\lib" -Filter '*cuda*'
```

For a CUDA wheel, files such as `ggml-cuda.dll` should be present.

## Step 9: Optional GGUF Smoke Test

Put a small GGUF model in `ComfyUI\models\LLM`, then run:

```powershell
$model = 'E:\ComfyUI-Easy-Install\ComfyUI\models\LLM\your-model.gguf'

& $py -c "from llama_cpp import Llama; import sys; model=sys.argv[1]; llm=Llama(model_path=model, n_gpu_layers=-1, n_ctx=512, verbose=True); print(llm('Say OK', max_tokens=8)['choices'][0]['text'])" $model
```

If the wheel is wrong, this is where failures usually become obvious.

## Common Problems

### `DLL load failed`

Most often caused by a wheel that does not match the Python ABI or by missing runtime DLLs. Recheck Python version and the wheel filename.

### `No module named llama_cpp`

The wheel was installed into the wrong Python. Use ComfyUI's embedded `python.exe`, not system Python.

### It imports, but local GGUF generation fails

Check that the model file is a real `.gguf`, that it is under `ComfyUI\models\LLM`, and that any matching vision projector file is in the same folder when using multimodal models.

### It runs on CPU or is extremely slow

Check the wheel CUDA selector, verify `ggml-cuda.dll` exists, and test with `n_gpu_layers=-1`.

### pip tries to replace Torch or NumPy

Cancel and reinstall with:

```powershell
& $py -m pip install --no-cache-dir --force-reinstall --no-deps <wheel-url>
```

## Future Installer / Doctor Tool

The included `tools/llama_cpp_windows_doctor.py` follows this model: doctor first, installer second.

It should:

1. Ask for or auto-detect the ComfyUI root.
2. Locate the embedded `python.exe`.
3. Detect Python ABI with `sys.version_info`.
4. Detect Torch version, `torch.version.cuda`, CUDA availability, and GPU name.
5. Run `nvidia-smi` when available and record driver/runtime capability.
6. Inspect the existing `llama-cpp-python` install and `direct_url.json`.
7. Query JamePeng GitHub releases.
8. Select a wheel matching `cpXXX`, `cuXXX`, and `win_amd64`.
9. Show the selected wheel and ask before changing the install.
10. Install with `pip install --force-reinstall --no-deps`.
11. Verify import, DLL path, CUDA DLL presence, and optionally run a GGUF smoke test.
12. Write a report with the detected state, selected wheel, install result, and next action.

The tool should never assume the system drive, never use system Python, and never upgrade unrelated packages unless the user explicitly asks for that.
