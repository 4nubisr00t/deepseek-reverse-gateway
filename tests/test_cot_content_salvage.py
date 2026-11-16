import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.engine.tool_bridge import ToolBridge
from app.engine.session_tree import SessionTracker, SessionContext


def test_salvage_content_with_code_block():
    thinking_text = """
    We need to solve this problem.
    Let's write a C program.
    The algorithm uses symmetry check across the origin and axes.
    ```c
    #include <stdio.h>
    #include <stdbool.h>

    int main() {
        printf("Hello World\\n");
        return 0;
    }
    ```
    Therefore, the time complexity is O(N log N) which fits within the 1.0s limit.
    """
    salvaged = ToolBridge.salvage_content_from_thinking(thinking_text)
    assert salvaged != ""
    assert "【网关打捞提示】" in salvaged
    assert "#include <stdio.h>" in salvaged
    assert "int main()" in salvaged
    assert "```c" in salvaged


def test_salvage_content_with_numbered_code():
    thinking_text = """
    Let me draft the solution:
    ```c
    1: #include <stdio.h>
    2: int main() {
    3:     return 0;
    4: }
    ```
    This completes the proof and code implementation.
    """
    salvaged = ToolBridge.salvage_content_from_thinking(thinking_text)
    assert salvaged != ""
    assert "#include <stdio.h>" in salvaged
    # Line numbers should be stripped
    assert "1: #" not in salvaged


def test_salvage_content_empty_or_trivial():
    # Only rambling thoughts without code or substantial conclusion
    thinking_text = "Wait, let me think. No, that's wrong. Maybe A? Or B?"
    salvaged = ToolBridge.salvage_content_from_thinking(thinking_text)
    assert salvaged == ""

    # Completely empty thinking
    assert ToolBridge.salvage_content_from_thinking("") == ""
    assert ToolBridge.salvage_content_from_thinking("   ") == ""


def test_session_tracker_reset_upstream_session():
    tracker = SessionTracker()
    conv_key = "test_conv_user_123"
    
    # Simulate an established session
    tracker.register_session(
        conv_key=conv_key,
        account_label="test_account_A",
        deepseek_session_id="ds_session_exhausted_999",
    )
    ctx = tracker.get_session(conv_key)
    assert ctx is not None
    ctx.turn_count = 23
    ctx.last_message_id = 12345

    # Trigger reset
    tracker.reset_upstream_session(conv_key, reason="upstream_thinking_only")

    # Verify context still exists with account_label preserved, but upstream session cleared
    saved_ctx = tracker.get_session(conv_key)
    assert saved_ctx is not None
    assert saved_ctx.account_label == "test_account_A"
    assert saved_ctx.deepseek_session_id == ""
    assert saved_ctx.last_message_id is None
    assert saved_ctx.turn_count == 0

