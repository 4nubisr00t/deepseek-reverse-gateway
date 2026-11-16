import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests.test_native_toolformats as m

m.test_incident_native_token_leakage_fixed()
m.test_chinese_smart_quotes_repaired()
m.test_suspicion_detects_unclosed_native_token()
m.test_two_paths_agree_on_arguments()
for label, body in m.BODIES.items():
    m.test_parse_invoke_body_extracts_path(label, body)
for label, block in m.FULL_BLOCKS.items():
    m.test_safety_net_extracts_args_for_all_formats(label, block)
    m.test_streaming_parser_extracts_args_for_all_formats(label, block)
print("ALL 23 NATIVE TOOL FORMAT TESTS PASSED SUCCESSFULLY ON SERVER!")
