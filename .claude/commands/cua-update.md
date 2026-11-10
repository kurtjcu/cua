---
description: Update cua to the latest version, and show the current version
---
Run `python3 -m cua update` and report the result.

If it says there is no git remote configured, explain that updates pull from the
checkout's `origin` remote, so one needs to be added before `/cua-update` can do
anything — and do not add a remote or push anything without the user asking.
To just see the version without updating, run `python3 -m cua version`.
