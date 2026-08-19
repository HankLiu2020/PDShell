from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import gradio as gr
except ImportError:
    gr = None

try:
    from .transport import FileTransport, JobSnapshot, TransportError, make_transport
except ImportError:
    from transport import FileTransport, JobSnapshot, TransportError, make_transport


def _default_endpoint() -> str:
    configured = os.getenv("PDSHELL_ENDPOINT")
    if configured:
        return configured
    host = os.getenv("PDSHELL_SSH_HOST")
    remote_root = os.getenv("PDSHELL_REMOTE_ROOT")
    if host and remote_root:
        user = os.getenv("PDSHELL_SSH_USER")
        return f"{user + '@' if user else ''}{host}:{remote_root}"
    return str(Path(__file__).resolve().parents[1] / "tasks")


def _format_updated(snapshot: JobSnapshot) -> str:
    if not snapshot.updated_at:
        return "-"
    return datetime.fromtimestamp(snapshot.updated_at, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _table_rows(snapshots: list[JobSnapshot]) -> list[list[str]]:
    return [
        [item.job_id, item.state, item.exitcode or "-", _format_updated(item), "🗑️ 删除"]
        for item in snapshots
    ]


def _upload_path(value: object) -> Path:
    if isinstance(value, str):
        return Path(value)
    if isinstance(value, dict) and value.get("path"):
        return Path(str(value["path"]))
    if hasattr(value, "name"):
        return Path(str(value.name))
    raise ValueError("没有选择 Shell 脚本")


def _suggest_job_id(value: object, current: str | None = "") -> str:
    current = current or ""
    if current.strip():
        return current
    try:
        stem = _upload_path(value).stem
    except (TypeError, ValueError, OSError):
        return current
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)
    base = re.sub(r"^[^A-Za-z0-9]+", "", base).strip("._-") or "task"
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    base = base[: max(1, 128 - len(timestamp) - 1)]
    return f"{base}-{timestamp}"


def submit_source(
    transport: FileTransport,
    upload: object,
    paste_script_value: str | None,
    job_id: str | None = "",
) -> str:
    if transport.read_only:
        raise TransportError("当前是 scp 只读模式，不能提交任务")
    selected = (job_id or "").strip() or None
    if upload not in (None, ""):
        script = _upload_path(upload)
        selected = selected or _suggest_job_id(upload)
        return transport.submit_script(script, selected)
    script_text = paste_script_value or ""
    if not script_text.strip():
        raise ValueError("请选择脚本文件或粘贴 Shell 脚本内容")
    with tempfile.TemporaryDirectory(prefix="pdshell-paste-") as temporary:
        script = Path(temporary) / "run.sh"
        script.write_text(script_text if script_text.endswith("\n") else script_text + "\n", encoding="utf-8")
        script.chmod(0o755)
        return transport.submit_script(script, selected)


def build_demo(transport: FileTransport, poll_interval: float = 2.0):
    if gr is None:
        raise RuntimeError("Gradio 未安装，请执行 pip install -r frontend/requirements.txt")

    def refresh(selected: str):
        sync_error = ""
        try:
            transport.sync_metadata()
        except TransportError as exc:
            sync_error = f" · 同步警告：{exc}（继续显示本地缓存）"
        snapshots = transport.snapshots()
        stdout = stderr = ""
        if selected:
            try:
                transport.sync_job(selected)
            except TransportError as exc:
                sync_error = f" · 日志同步警告：{exc}（继续显示本地缓存）"
            stdout, stderr = transport.logs(selected)
        return (
            transport.health() + sync_error,
            _table_rows(snapshots),
            f"### 任务日志：{selected or '未选择'}",
            stdout,
            stderr,
        )

    def select_job(event: gr.SelectData):
        index = getattr(event, "index", None)
        if isinstance(index, (tuple, list)):
            index = index[0]
        try:
            transport.sync_metadata()
        except TransportError:
            pass
        snapshots = transport.snapshots()
        if not isinstance(index, int) or index < 0 or index >= len(snapshots):
            return "", "### 任务日志：未选择", "", ""
        job_id = snapshots[index].job_id
        try:
            transport.sync_job(job_id)
        except TransportError:
            pass
        stdout, stderr = transport.logs(job_id)
        return job_id, f"### 任务日志：{job_id}", stdout, stderr

    def submit_script(upload: object, paste_script_value: str, job_id: str):
        try:
            created = submit_source(transport, upload, paste_script_value, job_id)
            return f"✅ 已提交 `{created}`；Worker 将在下一轮扫描中领取。"
        except (OSError, ValueError, TransportError) as exc:
            return f"❌ 提交失败：`{exc}`"

    def delete_selected(job_id: str, confirmed: bool):
        if transport.read_only:
            stdout, stderr = transport.logs(job_id) if job_id else ("", "")
            return (
                "⚠️ 当前是 scp 只读模式，不能删除任务。",
                job_id,
                transport.health(),
                _table_rows(transport.snapshots()),
                f"### 任务日志：{job_id or '未选择'}",
                stdout,
                stderr,
                confirmed,
            )
        if not job_id:
            return (
                "⚠️ 请先点击任务列表中的一行。",
                job_id,
                transport.health(),
                _table_rows(transport.snapshots()),
                "### 任务日志：未选择",
                "",
                "",
                confirmed,
            )
        if not confirmed:
            stdout, stderr = transport.logs(job_id)
            return (
                "⚠️ 删除不可恢复，请先勾选确认框。",
                job_id,
                transport.health(),
                _table_rows(transport.snapshots()),
                f"### 任务日志：{job_id}",
                stdout,
                stderr,
                confirmed,
            )
        try:
            transport.delete_job(job_id)
        except TransportError as exc:
            stdout, stderr = transport.logs(job_id)
            return (
                f"❌ 删除失败：`{exc}`",
                job_id,
                transport.health(),
                _table_rows(transport.snapshots()),
                f"### 任务日志：{job_id}",
                stdout,
                stderr,
                confirmed,
            )
        return (
            f"✅ 已提交删除请求 `{job_id}`；Worker 将清理任务目录和审计脚本。",
            "",
            transport.health(),
            _table_rows(transport.snapshots()),
            "### 任务日志：未选择",
            "",
            "",
            False,
        )

    with gr.Blocks(title="PDShell") as demo:
        selected_job = gr.State("")
        with gr.Row():
            with gr.Column(scale=1, min_width=300):
                gr.Markdown("## 上传脚本")
                upload = gr.File(label="Shell 脚本", file_types=[".sh"], type="filepath")
                paste_script = gr.Code(
                    label="或直接粘贴 Shell 脚本",
                    language="python",
                    lines=12,
                    interactive=not transport.read_only,
                )
                job_id = gr.Textbox(label="任务 ID（留空自动生成）", placeholder="例如 train-001")
                submit = gr.Button("提交任务", variant="primary", interactive=not transport.read_only)
                submission = gr.Markdown("当前传输模式为 scp 只读。" if transport.read_only else "准备好脚本后提交。")
                gr.Markdown("## 任务监控")
                health = gr.Markdown("正在读取 Worker 状态…")
                refresh_button = gr.Button("立即刷新")
                table = gr.Dataframe(
                    headers=["任务 ID", "状态", "退出码", "更新时间", "操作"],
                    datatype=["str", "str", "str", "str", "str"],
                    value=[],
                    row_count=0,
                    interactive=False,
                    wrap=True,
                )
                delete_confirm = gr.Checkbox(
                    label="确认删除选中的任务（不可恢复）",
                    value=False,
                    interactive=not transport.read_only,
                )
                delete_button = gr.Button(
                    "🗑️ 删除选中任务",
                    variant="stop",
                    interactive=not transport.read_only,
                )
                delete_status = gr.Markdown(
                    "scp 模式为只读，删除按钮已禁用。" if transport.read_only else "先点击任务行，再确认删除。"
                )
            with gr.Column(scale=2, min_width=500):
                log_header = gr.Markdown("### 任务日志：未选择")
                with gr.Tabs():
                    with gr.Tab("stdout"):
                        stdout = gr.Code(language="python", lines=30, interactive=False)
                    with gr.Tab("stderr"):
                        stderr = gr.Code(language="python", lines=30, interactive=False)

        submit.click(submit_script, inputs=[upload, paste_script, job_id], outputs=[submission])
        upload.change(_suggest_job_id, inputs=[upload, job_id], outputs=[job_id])
        table.select(select_job, inputs=[], outputs=[selected_job, log_header, stdout, stderr])
        delete_button.click(
            delete_selected,
            inputs=[selected_job, delete_confirm],
            outputs=[delete_status, selected_job, health, table, log_header, stdout, stderr, delete_confirm],
        )
        refresh_button.click(refresh, inputs=[selected_job], outputs=[health, table, log_header, stdout, stderr])
        if hasattr(gr, "Timer"):
            timer = gr.Timer(value=poll_interval)
            timer.tick(refresh, inputs=[selected_job], outputs=[health, table, log_header, stdout, stderr])

    return demo


def main() -> int:
    parser = argparse.ArgumentParser(description="PDShell local Gradio console")
    parser.add_argument("--mode", choices=["local", "rsync", "scp"], default=os.getenv("PDSHELL_TRANSPORT", "local"))
    parser.add_argument("--endpoint", default=_default_endpoint())
    parser.add_argument("--ssh-port", type=int, default=int(os.getenv("PDSHELL_SSH_PORT", "22")))
    parser.add_argument("--cache", type=Path, default=Path(os.getenv("PDSHELL_CACHE", ".pdshell-cache")))
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("PDSHELL_POLL_INTERVAL", "2")))
    parser.add_argument("--port", type=int, default=int(os.getenv("PDSHELL_PORT", "7860")))
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval 必须大于 0")
    if args.ssh_port <= 0 or args.ssh_port > 65535:
        parser.error("--ssh-port 必须介于 1 和 65535")
    transport = make_transport(
        args.mode,
        args.endpoint,
        args.cache,
        ssh_port=args.ssh_port,
        password=os.getenv("PDSHELL_SSH_PASSWORD"),
    )
    if gr is None:
        print("Gradio 未安装，请执行 pip install -r frontend/requirements.txt", file=sys.stderr)
        return 2
    build_demo(transport, args.poll_interval).launch(server_name="127.0.0.1", server_port=args.port, share=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
