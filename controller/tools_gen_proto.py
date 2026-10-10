"""Regenerate the Python gRPC stubs from proto/ (run from controller/: uv run python tools_gen_proto.py)."""

import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent
proto = root.parent / "proto"
out = root / "switchyard" / "adapters" / "agentpb"
out.mkdir(parents=True, exist_ok=True)
subprocess.run(
    [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{proto}",
        f"--python_out={out}",
        f"--grpc_python_out={out}",
        f"--pyi_out={out}",
        str(proto / "switchyard/agent/v1/agent.proto"),
    ],
    check=True,
)
src = out / "switchyard" / "agent" / "v1"
for name in ("agent_pb2.py", "agent_pb2.pyi", "agent_pb2_grpc.py"):
    text = (src / name).read_text()
    text = re.sub(
        r"from switchyard\.agent\.v1 import", "from switchyard.adapters.agentpb import", text
    )
    (out / name).write_text(text.replace("\r\n", "\n"), newline="\n")
import shutil

shutil.rmtree(out / "switchyard")
(out / "__init__.py").write_text(
    '"""Generated gRPC stubs for the SE agent (see tools_gen_proto.py)."""\n'
)
