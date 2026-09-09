---
layout: default
title: Privilege Protocol
parent: Internals
nav_order: 3
---

# Privilege Protocol

NativeDev uses a versioned protocol between the application and privileged helper.

Current protocol version:

```
23
```

## Purpose

Protocol versioning prevents incompatible application and helper versions from executing privileged operations.

Mismatch results in rejection.
