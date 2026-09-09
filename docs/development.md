# Development Guide

## Requirements

- Debian/Ubuntu family Linux
- Python 3
- GTK4 bindings
- Polkit

## Running

```bash
./run.sh
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Code Organization

```
src/nativedev/

controller.py
    Application workflow coordination

gui.py
    GTK interface

system.py
    Controlled system operations

privileged_helper.py
    Root operations
```
