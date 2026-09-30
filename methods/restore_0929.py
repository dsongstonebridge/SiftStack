"""Go back to the 9:29 foreclosure method. See methods/README.md.

Puts the 9:29 copy of the petition-info-extraction skill back in place. The
9:30 tools (src/batch_ocr.py, src/merge_petition_rows.py, the petition-extractor
agent) are separate files the 9:29 method never used, so they are simply not
used; nothing else needs undoing. The current skill is saved beside the
snapshot first, so switching back is itself reversible.
"""

import filecmp
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(HERE, "0929", "petition-info-extraction.SKILL.md")
LIVE = os.path.join(os.path.expanduser("~"), ".claude", "skills", "petition-info-extraction", "SKILL.md")
SAVED_0930 = os.path.join(HERE, "0930", "petition-info-extraction.SKILL.md")

if filecmp.cmp(SNAPSHOT, LIVE, shallow=False):
    print("Skill is already the 9:29 version.")
else:
    os.makedirs(os.path.dirname(SAVED_0930), exist_ok=True)
    shutil.copy2(LIVE, SAVED_0930)
    shutil.copy2(SNAPSHOT, LIVE)
    print(f"Restored the 9:29 skill. The 9:30 version was saved to {SAVED_0930}")
print("Now on the 9:29 method: read petitions directly per the skill; do not use "
      "batch_ocr.py, merge_petition_rows.py or the petition-extractor agent.")
