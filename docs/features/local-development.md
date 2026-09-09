---
layout: default
title: Local Development
parent: Features
nav_order: 1
---

# Local Development

![Local Development](../images/2.local-development.png)

NativeDev provides local project management with native Linux services.

## Features

- Project directory management
- `.test` local domains
- Nginx integration
- Framework web root detection
- PHP-FPM routing
- HTTPS support through mkcert

## Request Flow

```
Browser
 |
Nginx
 |
PHP-FPM
 |
Project
```
