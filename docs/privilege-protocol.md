# Privilege Protocol

## Purpose

The privilege protocol defines the communication contract between the GTK application and the root-owned helper.

## Current Version

```
23
```

## Transport

Communication uses structured messages through the Polkit privileged helper.

Flow:

```
Application
    |
 system.py
    |
 pkexec
    |
 privileged_helper.py
```

## Compatibility

The application and helper must use the same protocol version.

If versions differ, the request fails closed.

This prevents a newer GUI from sending unsupported operations to an older privileged helper.

## Message Concept

A request contains:

- Protocol version
- Action name
- Operation parameters

The helper validates the action before performing any system change.
