---
layout: default
title: Installation
parent: Getting Started
nav_order: 1
---

# Installation

## Supported Systems

NativeDev currently supports Debian and Ubuntu based distributions.

Support for additional distributions is planned for future releases.

## Install from Release

Download the latest `.deb` package from GitHub Releases.

```bash
sudo apt install ./nativedev_<version>_all.deb
```

## Source Installation

For contributors:

```bash
./install.sh
```

## Requirements

NativeDev requires:

- Python 3
- GTK runtime provided by distribution packages
- Polkit support
