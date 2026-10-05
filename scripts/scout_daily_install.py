"""Install a committed Scout version separately from development; no provider calls."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from quantlab.scout.models import fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-dir", type=Path)
    parser.add_argument("--canonical-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--releases-dir", type=Path, required=True)
    parser.add_argument("--desktop-dir", type=Path, required=True)
    args = parser.parse_args()
    git = ["git"] + ([f"--git-dir={args.git_dir}"] if args.git_dir else [])
    commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    subprocess.run(git + ["diff", "--exit-code", "HEAD"], check=True, stdout=subprocess.DEVNULL)
    release = args.releases_dir / commit
    if release.exists():
        raise ValueError("Version directory already exists; preserve it, do not replace")
    if any(c in str(release) for c in "'\"$;\n\r" + chr(96)):
        raise ValueError("Unsafe release path")
    if args.output_dir.resolve().is_relative_to(args.canonical_dir.resolve()):
        raise ValueError("Output cannot be inside canonical data")
    release.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "source.tar"
        subprocess.run(git + ["archive", "HEAD", "-o", str(archive)], check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(release)], check=True)
    subprocess.run(["uv", "sync", "--locked"], cwd=release, check=True)
    settings = {
        "release_root": str(release),
        "canonical_dir": str(args.canonical_dir),
        "output_root": str(args.output_dir),
        "state_root": str(args.state_dir),
    }
    (release / "daily-settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    files = {
        str(p.relative_to(release)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in release.rglob("*")
        if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts
    }
    dependencies = json.loads(
        subprocess.check_output(
            [
                str(release / ".venv/bin/python"),
                "-c",
                "import json,importlib.metadata as m; "
                "print(json.dumps({d.metadata['Name']:d.version "
                "for d in m.distributions()}))",
            ],
            text=True,
            cwd=release,
        )
    )
    manifest = {
        "commit": commit,
        "files": files,
        "settings_sha256": fingerprint(settings),
        "config_sha256": files["config/scout_daily.fixed.json"],
        "dependencies": dependencies,
    }
    (release / "daily-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for relative in files:
        (release / relative).chmod(0o444)
    (release / "daily-manifest.json").chmod(0o444)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    args.desktop_dir.mkdir(parents=True, exist_ok=False)
    shell = (
        "set -a; source /home/administrator/.config/quantlab/scout.env; set +a; "
        f"cd '{release}'; exec '{release}/.venv/bin/python' -m quantlab.scout.daily_runtime "
        f"--serve --settings '{release}/daily-settings.json'"
    )
    powershell = f'''$ErrorActionPreference = 'Stop'
$taskUrl = 'http://127.0.0.1:8766'
$taskReady = $false
try {{
    $taskState = Invoke-RestMethod "$taskUrl/state" -TimeoutSec 2
    if ($taskState.version -and $taskState.version -ne '{commit}') {{
        throw '另一个固定版本正在提供服务；请先关闭旧服务后重新打开。'
    }}
    $taskReady = $true
}} catch {{
    if ($_.Exception.Message -like '*另一个固定版本*') {{ throw }}
}}
if (-not $taskReady) {{
    $taskWslArguments = @'
-d Ubuntu -- bash -lc "{shell}"
'@
    Start-Process -FilePath 'wsl.exe' -WindowStyle Hidden -ArgumentList $taskWslArguments
    for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {{
        Start-Sleep -Seconds 1
        try {{
            Invoke-RestMethod "$taskUrl/state" -TimeoutSec 2 | Out-Null
            $taskReady=$true
            break
        }} catch {{}}
    }}
}}
if (-not $taskReady) {{ throw 'Scout服务未启动，请查看操作说明。' }}
Start-Process "$taskUrl/?start=1"
'''
    (args.desktop_dir / "launcher.ps1").write_text(powershell, encoding="utf-8-sig")
    (args.desktop_dir / "一键预测.cmd").write_text(
        "@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass "
        '-File "%~dp0launcher.ps1"\r\n',
        encoding="ascii",
    )
    (args.desktop_dir / "操作说明.md").write_text(
        "# Scout 日常预测\n\n双击 **一键预测.cmd**。\n"
        "入口自动检查交易日和行情，显示进度并打开报告。\n\n"
        f"固定版本：{commit}。日常运行只用独立发布目录，开发工作树修改不影响该版本。\n\n"
        "- 重复点击复用同一本日任务，包括失败记录，不再次付费。\n"
        "- 假日显示下一目标日与行情截至日；这不是当天可交易推荐。\n"
        "- 行情过期会在请求前停下。更新孤立行情副本后重新打开；不写原始数据。\n"
        "- 失败不会自动改配置或提高预算。修复、测试、提交后安装另一个版本。\n"
        "- 最多四请求、一次纠错；单请求三分钟、全流程三十分钟。\n"
        "- 新报告与请求原档保存在本机；旧报告保留。报告完成不表示已证明赚钱能力。\n"
        "- 关浏览器不取消已开始的任务。电脑休眠或异常退出可能导致交付未知，不自动补发。\n"
        "- 服务启动失败：检查WSL Ubuntu可用、端口8766未被其他应用占用。\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "release": str(release),
                "desktop": str(args.desktop_dir),
                "manifest_sha256": hashlib.sha256(
                    (release / "daily-manifest.json").read_bytes()
                ).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
