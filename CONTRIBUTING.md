# Contributing

Thanks for helping improve `cc-tmux`.

## Development

```bash
python -m pip install -e .[dev]
pytest
```

Please keep the CLI dependency-light, avoid shell injection risks, and include tests for behavior that does not require a live tmux/Claude installation.
