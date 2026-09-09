---
layout: default
title: Security Model
parent: Internals
nav_order: 2
---

# Security Model

NativeDev follows a strict privilege boundary.

## Principles

- Application runs as normal user.
- Root operations require explicit authorization.
- Privileged helper performs limited actions.
- No arbitrary root shell execution.

## Privilege Flow

```
User Application
      |
    Polkit
      |
Privilege Helper
      |
System Change
```
