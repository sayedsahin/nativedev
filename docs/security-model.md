# NativeDev Security Model

## Privilege Boundary

NativeDev separates normal user operations from privileged system changes.

```
User GUI
   |
   v
Structured request
   |
   v
Polkit helper
   |
   v
System change
```

## Rules

The privileged helper:

- Accepts predefined actions only
- Does not execute arbitrary shell commands
- Validates paths and parameters
- Does not trust GUI input

## Root Operations

Examples of semantic operations:

- Install package
- Configure service
- Apply owned configuration
- Update NativeDev package

Not supported:

- Arbitrary sudo commands
- Arbitrary file writes
- Arbitrary SQL execution

## Ownership

NativeDev writes its own managed files and avoids modifying unrelated user configuration whenever possible.
