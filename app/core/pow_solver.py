"""
DeepSeek PoW Challenge Solver (Node.js + WASM)
Provides async and sync solvers to avoid blocking FastAPI event loop.
"""

import os
import json
import base64
import asyncio
import subprocess
from typing import Dict, Any, Optional

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
POW_SOLVER_JS = os.path.join(CORE_DIR, "pow_solver.js")


class DeepSeekPOW:
    """Solves DeepSeek PoW challenge using Node.js WASM bridge."""

    @classmethod
    async def solve_async(cls, config: Dict[str, Any]) -> str:
        """Solve challenge in thread pool to prevent event loop blocking."""
        return await asyncio.to_thread(cls.solve, config)

    @classmethod
    def solve(cls, config: Dict[str, Any]) -> str:
        """Synchronously solve PoW challenge and return base64-encoded header."""
        input_json = json.dumps(config)
        try:
            result = subprocess.run(
                ["node", POW_SOLVER_JS, input_json],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=CORE_DIR,
            )
            if result.returncode == 0 and (result.stdout or "").strip():
                # stdout is base64-encoded response
                return result.stdout.strip()
            else:
                err_msg = (result.stderr or "").strip()[:200]
                raise RuntimeError(f"PoW solve failed (code={result.returncode}): {err_msg}")
        except FileNotFoundError:
            raise RuntimeError("Node.js is required to solve DeepSeek PoW challenges, but 'node' was not found.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("PoW challenge solving timed out after 30 seconds.")
