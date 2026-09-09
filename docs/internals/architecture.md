---
layout: default
title: Architecture
parent: Internals
nav_order: 1
---

# Architecture

```
NativeDev Application
        |
Controller
        |
Managers
        |
System Integration Layer
        |
Privilege Helper
        |
Linux Resources
```

## Components

### Controller

Coordinates operations between management components.

### Managers

Domain-specific components:

- PHP
- Node.js
- Services
- Projects

### System Layer

Handles:

- Packages
- systemd
- Filesystem
- Network configuration
