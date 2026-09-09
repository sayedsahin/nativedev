# NativeDev Architecture

## Overview

NativeDev is a GTK4 desktop application that acts as a management layer over the native Linux development environment.

The operating system remains the source of truth.

## Component Model

```
GTK Application
       |
       v
Controller
       |
       v
Feature Managers
       |
       v
System Abstraction
       |
       v
Privileged Helper (Polkit)
       |
       v
Linux packages, services and filesystem
```

## Main Components

### GUI Layer

Responsible for:

- User interaction
- Displaying state
- Requesting operations

The GUI does not perform privileged operations directly.

### Controller

Coordinates UI actions and application workflows.

### Managers

Feature-specific modules handle domains such as:

- PHP lifecycle
- Node lifecycle
- Services
- Projects
- Package operations

### System Layer

Provides a controlled interface for system operations.

### Privileged Helper

Runs through Polkit and performs approved root operations only.

## Design Principles

- Native packages over bundled runtimes
- System services over replacement stacks
- Explicit operations over arbitrary commands
- User ownership over development files
- Safe privilege boundaries
