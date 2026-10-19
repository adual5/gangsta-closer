---
description: Set, test or reset the Gangsta Closer sound clip (MP3, WAV or M4A, up to 5 MB)
argument-hint: "<path to clip> | test | reset"
allowed-tools: ["Bash(python3 ${CLAUDE_PLUGIN_ROOT}/hooks/closer.py clip:*)"]
---

```!
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/closer.py" clip "$ARGUMENTS"
```

Tell the user the result above in one line, in your own words. Don't run anything else.
