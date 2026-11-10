---
description: Show how to set up and use cua (the Claude usage audit toolkit)
---
Run `python3 -m cua help` and show the output to the user.

If they are new, walk them through the prerequisites (Python 3.9+, run from the
repo root, no `pip install` needed — it's pure stdlib) and the first command to
run (`python3 -m cua preflight`). Then offer to kick off `/cua-audit` (usage
audit) or `/cua-sdl` (SDL / change-management compliance).
